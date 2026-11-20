"""Cảnh báo theo ngưỡng gửi qua Telegram, Zalo, Slack và email — workspaces/<ten>/alerts.yaml.

    alerts:
      doanh_thu_giam:
        title: "Doanh thu hôm qua giảm mạnh"
        dataset: kpi                                   # tên trong datasets.yaml
        period: "hôm qua"                              # {{start}} {{end}} {{prev_start}} {{prev_end}} cho dataset
        params: {cua_hang: null}                       # tham số khác của dataset
        condition: "pct_change(doanh_thu, doanh_thu_truoc) <= -20"
        label: "Doanh thu so với hôm trước"            # tên chỉ số trong tin cảnh báo
        format: percent                                # number | currency | percent
        check: {at: "09:00"}                           # hoặc {every: 30m, between: "08:00-22:00"}
        recipients: ["role:admin", "telegram:123", "email:giamdoc@congty.vn"]
        dashboard: tong_quan                           # thêm link dashboard
        cooldown: null                                 # mặc định: mỗi người tối đa MỘT lần mỗi kỳ dữ liệu

Điều kiện là cú pháp khai báo, KHÔNG phải biểu thức Python: `<vế> <so sánh> <vế>`, mỗi vế là hằng số, tên cột,
`pct_change(cot, cot_ky_truoc)` (% thay đổi) hoặc tích của chúng (`0.8 * doanh_thu_truoc`, `80% * doanh_thu_truoc`).
Dataset trả nhiều dòng (vd tồn kho theo món) thì xét từng dòng, tin cảnh báo liệt kê các dòng vượt ngưỡng.
Mỗi người nhận chỉ nhận cảnh báo của dataset thuộc nguồn mình được xem.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bizagent.dashboards.engine import DashboardError
from bizagent.periods import PeriodError
from bizagent.reports import (
    DAYS,
    Sender,
    expand_recipients,
    fmt,
    make_sender,
    period_params,
    save_as_thread,
)
from bizagent.sources.base import SourceError
from bizagent.workspace import WorkspaceError

log = logging.getLogger(__name__)
_INTERVAL = re.compile(r"^(\d+)\s*([mhd])$")
_HHMM = re.compile(r"^\d{1,2}:\d{2}$")
MAX_ATTEMPTS = 3                                  # gửi lỗi: thử lại ở lần kiểm tra sau, tối đa 3 lần mỗi kỳ


def interval(text: str) -> timedelta:
    n, unit = _INTERVAL.match(text).groups()
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: int(n)})


# ----------------------------------------------------------------------------- điều kiện
class ConditionError(ValueError):
    pass


_TOKEN = re.compile(r"\s*(?:(?P<num>\d+(?:[.,]\d+)?%?)|(?P<op><=|>=|==|!=|<|>|≤|≥|≠|=)|(?P<name>[^\W\d]\w*)"
                    r"|(?P<punct>[-(),*]))")
_OP_ALIASES = {"≤": "<=", "≥": ">=", "≠": "!=", "=": "=="}
OP_TEXT = {"<": "<", "<=": "≤", ">": ">", ">=": "≥", "==": "=", "!=": "≠"}
FUNCTIONS = ("pct_change",)


@dataclass(frozen=True)
class Operand:
    """Hằng số, một cột, hoặc pct_change(cot, cot_ky_truoc)."""

    kind: Literal["num", "col", "pct"]
    number: float = 0.0
    cols: tuple[str, ...] = ()
    percent_sign: bool = False                      # viết dạng "80%"

    def value(self, row: dict[str, Any]) -> float | None:
        if self.kind == "num":
            return self.number
        vals = [_num(row.get(c)) for c in self.cols]
        if any(v is None for v in vals):
            return None
        if self.kind == "col":
            return vals[0]
        cur, prev = vals
        return None if prev == 0 else (cur - prev) / abs(prev) * 100

    def text(self) -> str:
        if self.kind == "num":
            n = self.number * (100 if self.percent_sign else 1)
            return (f"{n:g}%" if self.percent_sign else f"{n:g}").replace(".", ",")
        return self.cols[0] if self.kind == "col" else f"% thay đổi {self.cols[0]} so với {self.cols[1]}"


@dataclass(frozen=True)
class Side:
    factors: tuple[Operand, ...]                    # tích các thừa số

    def value(self, row: dict[str, Any]) -> float | None:
        out = 1.0
        for f in self.factors:
            v = f.value(row)
            if v is None:
                return None
            out *= v
        return out

    @property
    def columns(self) -> list[str]:
        return [c for f in self.factors for c in f.cols]

    @property
    def has_column(self) -> bool:
        return any(f.kind != "num" for f in self.factors)

    @property
    def is_pct(self) -> bool:
        return any(f.kind == "pct" for f in self.factors)

    def text(self) -> str:
        return " × ".join(f.text() for f in self.factors)


@dataclass(frozen=True)
class Condition:
    left: Side
    op: str
    right: Side

    @property
    def columns(self) -> list[str]:
        return list(dict.fromkeys(self.left.columns + self.right.columns))

    @property
    def is_pct(self) -> bool:
        return self.left.is_pct or self.right.is_pct

    def test(self, row: dict[str, Any]) -> tuple[bool, float | None, float | None]:
        """(vượt ngưỡng?, giá trị vế trái, giá trị vế phải). Thiếu dữ liệu (NULL) thì không vượt ngưỡng."""
        lv, rv = self.left.value(row), self.right.value(row)
        if lv is None or rv is None:
            return False, lv, rv
        ok = {"<": lv < rv, "<=": lv <= rv, ">": lv > rv, ">=": lv >= rv,
              "==": lv == rv, "!=": lv != rv}[self.op]
        return ok, lv, rv


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:
        return float(str(v))
    except ValueError:
        return None


def parse_condition(text: str) -> Condition:
    """Đọc điều kiện `<vế> <so sánh> <vế>`. Chỉ có cột, hằng số, `*`, pct_change(); không chạy code nào."""
    src = (text or "").strip()
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            if src[pos:].strip() == "":
                break
            raise ConditionError(f"Điều kiện '{src}': không hiểu ký tự '{src[pos:pos + 10]}'")
        kind = m.lastgroup
        tokens.append((kind, _OP_ALIASES.get(m.group(kind), m.group(kind))))
        pos = m.end()
    i = 0

    def peek() -> tuple[str, str] | None:
        return tokens[i] if i < len(tokens) else None

    def take(kind: str, value: str | None = None) -> str:
        nonlocal i
        t = peek()
        if t is None or t[0] != kind or (value is not None and t[1] != value):
            want = value or {"name": "tên cột", "num": "số", "op": "phép so sánh"}.get(kind, kind)
            raise ConditionError(f"Điều kiện '{src}': cần {want}")
        i += 1
        return t[1]

    def operand() -> Operand:
        nonlocal i
        t = peek()
        if t == ("punct", "-"):                     # số âm
            i += 1
            o = operand()
            if o.kind != "num":
                raise ConditionError(f"Điều kiện '{src}': dấu '-' chỉ đứng trước số")
            return Operand("num", -o.number, percent_sign=o.percent_sign)
        if t and t[0] == "num":
            i += 1
            raw = t[1].replace(",", ".")
            if raw.endswith("%"):
                return Operand("num", float(raw[:-1]), percent_sign=True)
            return Operand("num", float(raw))
        if t and t[0] == "name":
            name = take("name")
            if peek() == ("punct", "("):
                if name not in FUNCTIONS:
                    raise ConditionError(f"Điều kiện '{src}': không có hàm {name}() (chỉ có {', '.join(FUNCTIONS)})")
                take("punct", "(")
                a = take("name")
                take("punct", ",")
                b = take("name")
                take("punct", ")")
                return Operand("pct", cols=(a, b))
            if name.lower() in ("and", "or", "not", "va", "hoac"):
                raise ConditionError(f"Điều kiện '{src}': chỉ một phép so sánh, không dùng '{name}'")
            return Operand("col", cols=(name,))
        raise ConditionError(f"Điều kiện '{src}': cần tên cột hoặc số")

    def side() -> Side:
        factors = [operand()]
        while peek() == ("punct", "*"):
            take("punct", "*")
            factors.append(operand())
        if sum(f.kind != "num" for f in factors) > 1:
            raise ConditionError(f"Điều kiện '{src}': mỗi vế chỉ nhân với hằng số (vd 0.8 * doanh_thu_truoc)")
        if len(factors) > 1:                        # "80% * cot" = 0.8 * cot
            factors = [Operand("num", f.number / 100, percent_sign=True) if f.kind == "num" and f.percent_sign
                       else f for f in factors]
        else:                                       # "ty_le < 30%" = so với 30 (điểm phần trăm)
            factors = [Operand("num", f.number) if f.kind == "num" else f for f in factors]
        return Side(tuple(factors))

    left = side()
    t = peek()
    if t is None or t[0] != "op":
        raise ConditionError(f"Điều kiện '{src}': cần phép so sánh (<, <=, >, >=, ==, !=)")
    op = take("op")
    right = side()
    if peek() is not None:
        raise ConditionError(f"Điều kiện '{src}': thừa '{peek()[1]}' (chỉ một phép so sánh)")
    if not (left.has_column or right.has_column):
        raise ConditionError(f"Điều kiện '{src}': cần ít nhất một cột")
    return Condition(left, op, right)


# ----------------------------------------------------------------------------- cấu hình
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckSchedule(_Strict):
    every: str | None = None                        # 30m, 1h, 1d
    at: str | None = None                           # "09:00" mỗi ngày (theo days)
    days: list[str] = Field(default_factory=lambda: list(DAYS))
    between: str | None = None                      # "08:00-22:00": chỉ kiểm tra trong khung giờ này (every)
    max_delay_minutes: int = 180                    # at: server bật lại muộn quá mức này thì bỏ qua hôm đó

    @model_validator(mode="after")
    def _check(self) -> CheckSchedule:
        if (self.every is None) == (self.at is None):
            raise ValueError("check cần đúng một trong every (vd 30m) / at (vd \"09:00\")")
        if self.every and not _INTERVAL.match(self.every):
            raise ValueError("every có dạng 30m, 2h, 1d")
        if self.every and interval(self.every) < timedelta(minutes=1):
            raise ValueError("every tối thiểu 1m")
        if self.at and not _HHMM.match(self.at):
            raise ValueError('at có dạng "09:00"')
        if self.between and not re.fullmatch(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", self.between):
            raise ValueError('between có dạng "08:00-22:00"')
        bad = [d for d in self.days if d not in DAYS]
        if bad:
            raise ValueError(f"days chỉ gồm {DAYS}")
        return self

    def describe(self) -> str:
        days = "" if len(self.days) == 7 else f" ({', '.join(self.days)})"
        if self.every:
            return f"mỗi {self.every}" + (f", {self.between}" if self.between else "") + days
        return f"lúc {self.at}" + days

    def _minutes(self, hhmm: str) -> int:
        h, m = map(int, hhmm.strip().split(":"))
        return h * 60 + m

    def due(self, now: datetime, last: datetime | None) -> bool:
        """Tới lượt kiểm tra chưa (now theo múi giờ workspace, last = lần kiểm tra trước)."""
        if DAYS[now.weekday()] not in self.days:
            return False
        if self.every:
            if self.between:
                lo, hi = (self._minutes(x) for x in self.between.split("-"))
                cur = now.hour * 60 + now.minute
                if not (lo <= cur <= hi if lo <= hi else cur >= lo or cur <= hi):
                    return False
            return last is None or now - last >= interval(self.every)
        h, m = map(int, self.at.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < slot or now - slot > timedelta(minutes=self.max_delay_minutes):
            return False
        return last is None or last < slot


class AlertSpec(_Strict):
    title: str = ""
    dataset: str
    period: str = "hôm nay"
    params: dict[str, Any] = Field(default_factory=dict)
    condition: str
    label: str | None = None                        # tên chỉ số trong tin (mặc định: vế trái điều kiện)
    format: Literal["number", "currency", "percent"] = "number"
    label_column: str | None = None                 # dataset nhiều dòng: cột tên dòng (vd mon, cua_hang)
    max_rows: int = 5                               # số dòng vượt ngưỡng tối đa liệt kê trong tin
    check: CheckSchedule = Field(default_factory=lambda: CheckSchedule(every="1h"))
    recipients: list[str]
    dashboard: str | None = None
    cooldown: str | None = None                     # vd 6h: gửi lại sau 6 giờ nếu vẫn vượt ngưỡng
    enabled: bool = True

    @field_validator("condition")
    @classmethod
    def _condition(cls, v: str) -> str:
        parse_condition(v)                          # ConditionError là ValueError -> lỗi cấu hình dễ đọc
        return v

    @field_validator("cooldown")
    @classmethod
    def _cooldown(cls, v: str | None) -> str | None:
        if v is not None and not _INTERVAL.match(v):
            raise ValueError("cooldown có dạng 30m, 6h, 1d")
        return v

    @property
    def cond(self) -> Condition:
        return parse_condition(self.condition)


class AlertsConfig(_Strict):
    alerts: dict[str, AlertSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names(self) -> AlertsConfig:
        for name, spec in self.alerts.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(f"tên cảnh báo '{name}' chỉ gồm chữ không dấu, số, _ và -")
            spec.title = spec.title or name
        return self


def load_alerts(ws) -> AlertsConfig:
    path = ws.path / "alerts.yaml"
    if not path.exists():
        return AlertsConfig()
    try:
        return AlertsConfig(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    except Exception as e:
        raise WorkspaceError(f"alerts.yaml không hợp lệ: {e}") from e


def check_alerts(ws, catalog=None) -> tuple[list[str], list[str]]:
    """(lỗi, lưu ý) của alerts.yaml khi đối chiếu với datasets/dashboards/người dùng (cho `bizagent check`).
    Người nhận chỉ là lưu ý: người dùng thêm trên web/chat nằm trong state DB, có thể chưa có lúc kiểm tra."""
    from bizagent.periods import resolve_period

    problems: list[str] = []
    notes: list[str] = []
    for name, spec in load_alerts(ws).alerts.items():
        if not spec.enabled:
            continue
        source = None
        if catalog is not None:
            if spec.dataset not in catalog.datasets:
                problems.append(f"{name}: không có dataset '{spec.dataset}' trong datasets.yaml")
            else:
                source = catalog.datasets[spec.dataset].source
            if spec.dashboard and spec.dashboard not in catalog.dashboards:
                problems.append(f"{name}: không có dashboard '{spec.dashboard}'")
        try:
            resolve_period(spec.period, date.today(), ws.info.week_start)
        except PeriodError as e:
            problems.append(f"{name}: period: {e}")
        targets = expand_recipients(ws, spec.recipients)
        if not targets:
            notes.append(f"{name}: chưa có người nhận nào (telegram:/zalo:/slack:/email: hoặc role: có người)")
        elif source is not None and not any(
                source in ws.sources_for(u, ws.role_of(u)) for u in targets if ws.role_of(u) is not None):
            notes.append(f"{name}: không người nhận nào được xem nguồn '{source}' của dataset {spec.dataset}")
    return problems, notes


# ----------------------------------------------------------------------------- kết quả kiểm tra
@dataclass
class Match:
    label: str | None
    value: float | None
    threshold: float | None


@dataclass
class AlertResult:
    fired: bool
    matches: list[Match]
    total_rows: int
    period_label: str
    period_key: str
    current: Match | None = None                    # giá trị dòng đầu (để xem khi không vượt ngưỡng)
    multi: bool = False                             # dataset nhiều dòng: tin liệt kê theo dòng


@dataclass
class AlertCheck:
    name: str
    result: AlertResult | None = None
    error: str | None = None
    status: dict[str, str] = field(default_factory=dict)       # người nhận -> đã gửi / bỏ qua / lỗi
    messages: dict[str, str] = field(default_factory=dict)     # người nhận -> nội dung tin (đã gửi hoặc sẽ gửi)

    def summary(self) -> str:
        if self.error:
            return f"lỗi: {self.error}"
        if self.result is None:
            return "không người nhận nào được xem dữ liệu này"
        r = self.result
        if not r.fired:
            return "không vượt ngưỡng" if r.total_rows else "dataset không có dữ liệu"
        sent = sum(1 for s in self.status.values() if s == "đã gửi")
        head = f"vượt ngưỡng ({len(r.matches)} dòng)" if r.multi else "vượt ngưỡng"
        return f"{head}, gửi {sent}/{len(self.status)} người"


# ----------------------------------------------------------------------------- kiểm tra và gửi
class AlertRunner:
    def __init__(self, service, senders: dict[str, Sender] | None = None):
        self.service = service
        self.ws = service.ws
        self.tz = ZoneInfo(self.ws.info.timezone)
        self.config = load_alerts(self.ws)
        self._senders = senders if senders is not None else {}

    def sender(self, channel: str) -> Sender:
        return make_sender(self.service, channel, self._senders)

    def recipients(self, spec: AlertSpec) -> list[str]:
        return expand_recipients(self.ws, spec.recipients)

    def _engine(self):
        engine = self.service.dashboard_engine
        if engine is None:
            raise WorkspaceError("Cảnh báo cần datasets.yaml (dashboards bị lỗi cấu hình)")
        return engine

    async def evaluate(self, spec: AlertSpec, user_id: str, role: str, today: date) -> AlertResult:
        """Chạy dataset dưới quyền của `user_id` (kiểm tra nguồn) rồi xét điều kiện trên từng dòng."""
        engine = self._engine()
        params, label, key = period_params(self.ws, spec.period, spec.params, today)
        r = await asyncio.to_thread(engine.run_dataset, spec.dataset, params, role, user_id=user_id,
                                    use_cache=False)
        cond = spec.cond
        missing = [c for c in cond.columns + ([spec.label_column] if spec.label_column else [])
                   if c not in r.columns]
        if missing:
            raise DashboardError(f"Dataset {spec.dataset} không có cột {', '.join(missing)} "
                                 f"(có: {', '.join(r.columns)})")
        label_col = spec.label_column
        if label_col is None and len(r.rows) > 1 and r.columns and r.columns[0] not in cond.columns:
            label_col = r.columns[0]
        matches: list[Match] = []
        current = None
        for raw in r.rows:
            row = dict(zip(r.columns, raw, strict=True))
            ok, lv, rv = cond.test(row)
            m = Match(None if label_col is None else str(row.get(label_col)), lv, rv)
            current = current or m
            if ok:
                matches.append(m)
        return AlertResult(bool(matches), matches, len(r.rows), label, key, current,
                           multi=label_col is not None)

    def message(self, spec: AlertSpec, result: AlertResult, user_id: str, role: str | None,
                two_way: bool = True) -> str:
        cond = spec.cond
        kind = "percent" if cond.is_pct else spec.format
        op = OP_TEXT[cond.op]
        name = spec.label or cond.left.text()
        rhs_note = f" ({cond.right.text()})" if cond.right.has_column else ""
        lines = [f"🚨 Cảnh báo: {spec.title}"]
        if not result.multi and result.matches:
            m = result.matches[0]
            lines.append(f"{name}: {fmt(m.value, kind)}")
            lines.append(f"Ngưỡng: {op} {fmt(m.threshold, kind)}{rhs_note}")
        else:
            rule = f"{op} {fmt(result.matches[0].threshold, kind)}" if not cond.right.has_column else \
                f"{op} {cond.right.text()}"
            lines.append(f"{name} {rule}: {len(result.matches)}/{result.total_rows} dòng")
            for m in result.matches[: spec.max_rows]:
                thr = f" (ngưỡng {fmt(m.threshold, kind)})" if cond.right.has_column else ""
                lines.append(f"• {m.label}: {fmt(m.value, kind)}{thr}")
            if len(result.matches) > spec.max_rows:
                lines.append(f"• … và {len(result.matches) - spec.max_rows} dòng khác")
        lines.append(f"Kỳ dữ liệu: {result.period_label}")
        cat = self.service.catalog
        if spec.dashboard and cat and cat.config.public_url and cat.can_view(spec.dashboard, role, user_id):
            lines.append(f"🔗 Dashboard: {cat.link(spec.dashboard)}")
        if two_way:
            lines.append("Nhắn tiếp để hỏi thêm.")
        return "\n".join(lines)

    def _suppressed(self, spec: AlertSpec, st: dict[str, Any], period_key: str, now: datetime) -> str | None:
        """Lý do không gửi lại (chống gửi lặp), hoặc None nếu được gửi."""
        if spec.cooldown:
            if st.get("sent_at"):
                last = datetime.fromisoformat(st["sent_at"])
                if now - last < interval(spec.cooldown):
                    return f"bỏ qua (đã gửi lúc {last.astimezone(self.tz):%H:%M %d/%m}, cooldown {spec.cooldown})"
        elif st.get("sent_period") == period_key:
            return "bỏ qua (đã gửi trong kỳ này)"
        if st.get("fail_period") == period_key and st.get("attempts", 0) >= MAX_ATTEMPTS:
            return f"bỏ qua (gửi lỗi {MAX_ATTEMPTS} lần trong kỳ này)"
        return None

    async def deliver(self, spec: AlertSpec, user_id: str, text: str) -> None:
        channel, chat_id = user_id.split(":", 1)
        sender = self.sender(channel)
        await sender.send(chat_id, text)            # email: dòng đầu làm tiêu đề
        if getattr(sender, "two_way", True):
            await save_as_thread(self.service, user_id, f"Cảnh báo: {spec.title}", text)

    async def check(self, name: str, now: datetime | None = None, *, send: bool = True, force: bool = False,
                    only: list[str] | None = None) -> AlertCheck:
        """Kiểm tra một cảnh báo và gửi cho người nhận (send=False: chỉ xem trước; force: bỏ qua chống gửi lặp)."""
        spec = self.config.alerts[name]
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        store = self.service.store
        out = AlertCheck(name)
        try:
            source = self._engine().catalog.dataset(spec.dataset).source
        except WorkspaceError as e:
            out.error = str(e)
            return out
        allowed: list[tuple[str, str]] = []
        for uid in only or self.recipients(spec):
            role = self.ws.role_of(uid)
            if role is None:
                out.status[uid] = "bỏ qua (chưa được cấp quyền)"
            elif source not in self.ws.sources_for(uid, role):
                out.status[uid] = f"bỏ qua (không xem được nguồn {source})"
            else:
                allowed.append((uid, role))
        if not allowed:
            return out
        try:
            # dữ liệu theo nguồn, không theo người: chạy một lần dưới quyền người nhận đầu tiên được xem
            out.result = result = await self.evaluate(spec, *allowed[0], now.date())
        except (SourceError, DashboardError, PeriodError, WorkspaceError) as e:
            out.error = str(e)
            return out
        for uid, role in allowed:
            if not result.fired:
                out.status[uid] = "không vượt ngưỡng"
                continue
            key = f"alert_sent:{name}:{uid}"
            st = await asyncio.to_thread(store.kv_get, key) or {}
            reason = None if force else self._suppressed(spec, st, result.period_key, now)
            if reason:
                out.status[uid] = reason
                continue
            channel = uid.split(":", 1)[0]
            try:
                two_way = getattr(self.sender(channel), "two_way", True)
            except RuntimeError as e:
                out.status[uid] = f"lỗi: {e}"
                continue
            text = out.messages[uid] = self.message(spec, result, uid, role, two_way)
            if not send:
                out.status[uid] = "sẽ gửi"
                continue
            try:
                await self.deliver(spec, uid, text)
            except Exception as e:
                log.error("Gửi cảnh báo %s cho %s lỗi: %s", name, uid, e)
                same = st.get("fail_period") == result.period_key
                st.update(fail_period=result.period_key, attempts=(st.get("attempts", 0) if same else 0) + 1,
                          error=str(e)[:300])
                out.status[uid] = f"lỗi: {e}"
            else:
                st = {"sent_period": result.period_key, "sent_at": now.astimezone(UTC).isoformat()}
                out.status[uid] = "đã gửi"
                await asyncio.to_thread(store.audit, uid, "alert_sent", alert=name, channel=channel,
                                        period=result.period_key)
            await asyncio.to_thread(store.kv_set, key, st)
        return out

    # ------------------------------------------------------------------ lịch
    async def _record(self, name: str, res: AlertCheck, now: datetime) -> None:
        retry = any(s.startswith("lỗi") for s in res.status.values()) or res.error is not None
        await asyncio.to_thread(self.service.store.kv_set, f"alert_check:{name}", {
            "at": now.astimezone(UTC).isoformat(), "summary": res.summary(), "retry": retry,
            "fired": bool(res.result and res.result.fired)})

    def last_check(self, name: str) -> dict[str, Any]:
        return self.service.store.kv_get(f"alert_check:{name}") or {}

    async def check_and_record(self, name: str, now: datetime | None = None, **kwargs: Any) -> AlertCheck:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        res = await self.check(name, now, **kwargs)
        await self._record(name, res, now)
        return res

    async def tick(self, now: datetime | None = None) -> dict[str, AlertCheck]:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        results: dict[str, AlertCheck] = {}
        for name, spec in self.config.alerts.items():
            if not spec.enabled:
                continue
            st = await asyncio.to_thread(self.last_check, name)
            last = datetime.fromisoformat(st["at"]).astimezone(self.tz) if st.get("at") else None
            # lần trước lỗi (nguồn mất kết nối, gửi lỗi): thử lại ở vòng sau thay vì chờ tới lịch kế tiếp
            retry = st.get("retry") and last is not None and now - last >= timedelta(minutes=1) \
                and DAYS[now.weekday()] in spec.check.days
            if not (retry or spec.check.due(now, last)):
                continue
            try:
                results[name] = await self.check_and_record(name, now)
            except Exception as e:
                log.exception("Kiểm tra cảnh báo %s lỗi", name)
                results[name] = AlertCheck(name, error=str(e))
                await self._record(name, results[name], now)
        return results

    async def run_forever(self) -> None:
        if not self.config.alerts:
            return
        log.info("Cảnh báo theo ngưỡng: %s", list(self.config.alerts))
        while True:
            try:
                self.config = await asyncio.to_thread(load_alerts, self.ws)     # sửa alerts.yaml là có hiệu lực
                await self.tick()
            except Exception:
                log.exception("Vòng cảnh báo lỗi")
            await asyncio.sleep(60)
