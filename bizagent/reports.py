"""Báo cáo định kỳ gửi qua kênh chat (Telegram, Zalo, Slack) — workspaces/<ten>/reports.yaml.

    reports:
      bao_cao_sang:
        title: "Báo cáo sáng"
        schedule: {at: "07:00"}                  # days: [mon, ..., sun] | day_of_month: 1
        period: "hôm qua"                         # {{start}} {{end}} {{prev_start}} {{prev_end}} cho dataset
        params: {cua_hang: null}                  # tham số khác của dataset (null = tất cả)
        recipients: ["telegram:123", "role:manager"]
        sections:
          - title: "Kết quả hôm qua"
            dataset: kpi
            kpis:
              - {label: "Doanh thu", value: doanh_thu, compare: doanh_thu_truoc, format: currency}
          - title: "Theo cửa hàng"
            dataset: theo_cua_hang
            table: {columns: [cua_hang, doanh_thu], max_rows: 5, formats: {doanh_thu: currency}}
        commentary: true                          # 2–3 câu nhận xét do AI viết; câu có số lạ bị bỏ
        dashboard: tong_quan                      # thêm link dashboard
        max_delay_minutes: 180                    # server bật lại muộn quá mức này thì bỏ qua hôm đó

Số liệu lấy bằng dataset (datasets.yaml) nên khớp với dashboard. Mỗi người nhận chỉ thấy mục thuộc nguồn mình
được xem. Sau khi gửi, báo cáo thành một cuộc trò chuyện mới của người nhận: nhắn tiếp là hỏi trong ngữ cảnh báo cáo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bizagent.dashboards.engine import DashboardError
from bizagent.numcheck import unverified
from bizagent.periods import resolve_period
from bizagent.sources.base import SourceError
from bizagent.workspace import WorkspaceError

log = logging.getLogger(__name__)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KpiLine(_Strict):
    label: str
    value: str
    compare: str | None = None
    format: Literal["number", "currency", "percent"] = "number"


class TableSpec(_Strict):
    columns: list[str] = Field(default_factory=list)
    max_rows: int = 10
    formats: dict[str, Literal["number", "currency", "percent"]] = Field(default_factory=dict)


class Section(_Strict):
    title: str
    dataset: str
    kpis: list[KpiLine] = Field(default_factory=list)
    table: TableSpec | None = None


class Schedule(_Strict):
    at: str = "07:00"
    days: list[str] = Field(default_factory=lambda: list(DAYS))
    day_of_month: int | None = None

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        if not re.fullmatch(r"\d{1,2}:\d{2}", v):
            raise ValueError('at có dạng "07:00"')
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d not in DAYS]
        if bad:
            raise ValueError(f"days chỉ gồm {DAYS}")
        return v

    def slot(self, day: date, tz: ZoneInfo) -> datetime | None:
        """Thời điểm gửi trong ngày `day`, hoặc None nếu hôm đó không gửi."""
        if self.day_of_month is not None:
            if day.day != self.day_of_month:
                return None
        elif DAYS[day.weekday()] not in self.days:
            return None
        h, m = map(int, self.at.split(":"))
        return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


class ReportSpec(_Strict):
    title: str
    schedule: Schedule = Field(default_factory=Schedule)
    period: str = "hôm qua"
    params: dict[str, Any] = Field(default_factory=dict)
    recipients: list[str]
    sections: list[Section]
    commentary: bool = False
    dashboard: str | None = None
    max_delay_minutes: int = 180
    enabled: bool = True


class ReportsConfig(_Strict):
    reports: dict[str, ReportSpec] = Field(default_factory=dict)


def load_reports(ws) -> ReportsConfig:
    path = ws.path / "reports.yaml"
    if not path.exists():
        return ReportsConfig()
    try:
        return ReportsConfig(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    except Exception as e:
        raise WorkspaceError(f"reports.yaml không hợp lệ: {e}") from e


# ----------------------------------------------------------------------------- định dạng số
def fmt(v: Any, kind: str = "number") -> str:
    if v is None:
        return "–"
    if not isinstance(v, (int, float)):
        return str(v)

    def vi(x: float, dec: int) -> str:
        s = f"{x:,.{dec}f}"
        return s.replace(",", "§").replace(".", ",").replace("§", ".")

    if kind == "percent":
        return vi(v, 1) + "%"
    if kind == "currency":
        a = abs(v)
        if a >= 1e9:
            return vi(v / 1e9, 2) + " tỷ"
        if a >= 1e6:
            return vi(v / 1e6, 1) + " triệu"
        return vi(v, 0) + " ₫"
    return vi(v, 0 if float(v).is_integer() or abs(v) >= 100 else 2)


def change(v: Any, p: Any) -> str:
    if not isinstance(v, (int, float)) or not isinstance(p, (int, float)) or p == 0:
        return ""
    d = (v - p) / abs(p) * 100
    return f" ({'▲' if d >= 0 else '▼'} {fmt(abs(d), 'percent')})"


# ----------------------------------------------------------------------------- người gửi
class Sender:
    async def send(self, chat_id: str, text: str) -> None:  # pragma: no cover - giao diện
        raise NotImplementedError


class TelegramSender(Sender):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(timeout=20, transport=transport)

    async def send(self, chat_id: str, text: str) -> None:
        from bizagent.channels.common import split_text

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("Chưa đặt TELEGRAM_BOT_TOKEN")
        for part in split_text(text, 4000):
            r = await self.http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
            if r.status_code != 200:
                raise RuntimeError(f"Telegram trả HTTP {r.status_code}")


class ZaloSender(Sender):
    def __init__(self, store):
        from bizagent.channels.zalo import ZaloClient

        self.client = ZaloClient(store)

    async def send(self, chat_id: str, text: str) -> None:
        await self.client.send_text(chat_id, text)


class SlackSender(Sender):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        from bizagent.channels.slack import SlackClient

        self.client = SlackClient(transport=transport)

    async def send(self, chat_id: str, text: str) -> None:
        await self.client.post_message(chat_id, text)


# ----------------------------------------------------------------------------- dựng và gửi
class ReportRunner:
    def __init__(self, service, senders: dict[str, Sender] | None = None):
        self.service = service
        self.ws = service.ws
        self.tz = ZoneInfo(self.ws.info.timezone)
        self.config = load_reports(self.ws)
        self._senders = senders or {}

    def sender(self, channel: str) -> Sender:
        if channel not in self._senders:
            factories = {"telegram": TelegramSender, "zalo": lambda: ZaloSender(self.service.store),
                         "slack": SlackSender}
            if channel not in factories:
                raise RuntimeError(f"Kênh '{channel}' không gửi được báo cáo (dùng telegram/zalo/slack)")
            self._senders[channel] = factories[channel]()
        return self._senders[channel]

    def recipients(self, spec: ReportSpec) -> list[str]:
        out: list[str] = []
        for r in spec.recipients:
            if r.startswith("role:"):
                out += [u.id for u in self.ws.all_users() if u.role == r[5:]]
            else:
                out.append(r)
        return [r for r in dict.fromkeys(out) if r.split(":", 1)[0] in ("telegram", "zalo", "slack")]

    def params(self, spec: ReportSpec, today: date) -> tuple[dict[str, Any], str]:
        p = resolve_period(spec.period, today, self.ws.info.week_start)
        prev = p.previous()
        label = p.start.strftime("%d/%m/%Y") if p.days == 1 else f"{p.start:%d/%m} – {p.end:%d/%m/%Y}"
        return {**spec.params, "start": p.start.isoformat(), "end": p.end.isoformat(),
                "prev_start": prev.start.isoformat(), "prev_end": prev.end.isoformat()}, label

    async def build(self, name: str, user_id: str, today: date | None = None) -> str | None:
        """Nội dung báo cáo cho một người nhận (None nếu người đó không được xem mục nào)."""
        spec = self.config.reports[name]
        role = self.ws.role_of(user_id)
        if role is None:
            return None
        engine = self.service.dashboard_engine
        if engine is None:
            raise WorkspaceError("Báo cáo cần datasets.yaml (dashboards bị lỗi cấu hình)")
        today = today or datetime.now(self.tz).date()
        params, label = self.params(spec, today)
        blocks: list[str] = []
        data_texts: list[str] = []
        for sec in spec.sections:
            ds = engine.catalog.dataset(sec.dataset)
            if ds.source not in self.ws.sources_for(user_id, role):
                continue
            try:
                r = await asyncio.to_thread(engine.run_dataset, sec.dataset, params, role, user_id=user_id,
                                            limit=max(50, sec.table.max_rows if sec.table else 1), use_cache=False)
            except (SourceError, DashboardError) as e:
                blocks.append(f"▪ {sec.title}\n  (tạm thiếu dữ liệu: {e})")
                continue
            data_texts.append(r.to_text())
            lines = [f"▪ {sec.title}"]
            row = dict(zip(r.columns, r.rows[0], strict=True)) if r.rows else {}
            for k in sec.kpis:
                cmp = change(row.get(k.value), row.get(k.compare)) if k.compare else ""
                lines.append(f"  {k.label}: {fmt(row.get(k.value), k.format)}{cmp}")
            if sec.table:
                cols = sec.table.columns or r.columns
                idx = [r.columns.index(c) for c in cols if c in r.columns]
                for i, rr in enumerate(r.rows[: sec.table.max_rows], 1):
                    cells = [fmt(rr[j], sec.table.formats.get(r.columns[j], "number")) for j in idx]
                    lines.append(f"  {i}. " + " · ".join(cells))
                if not r.rows:
                    lines.append("  (không có dữ liệu)")
            blocks.append("\n".join(lines))
        if not blocks:
            return None
        text = f"📊 {spec.title} — {label}\n\n" + "\n\n".join(blocks)
        if spec.commentary:
            note = await self.commentary(text, data_texts)
            if note:
                text += f"\n\n💬 {note}"
        cat = self.service.catalog
        if spec.dashboard and cat and cat.config.public_url and cat.can_view(spec.dashboard, role, user_id):
            text += f"\n\n🔗 Dashboard: {cat.link(spec.dashboard)}"
        return text + "\n\nNhắn tiếp để hỏi thêm về báo cáo này."

    async def commentary(self, report: str, data_texts: list[str]) -> str | None:
        """2–3 câu nhận xét do AI viết; có con số không đối chiếu được với báo cáo thì bỏ."""
        from langchain_core.messages import HumanMessage, SystemMessage

        from bizagent.agent.service import message_text

        try:
            msg = await self.service._model.ainvoke([
                SystemMessage(content="Bạn viết nhận xét ngắn (2-3 câu, tiếng Việt) cho báo cáo kinh doanh. "
                                      "Chỉ dùng con số có trong báo cáo, không suy diễn nguyên nhân không có căn cứ."),
                HumanMessage(content=report),
            ])
        except Exception as e:
            log.warning("Nhận xét báo cáo lỗi: %s", e)
            return None
        note = message_text(msg).strip()
        if not note or unverified(note, data_texts):
            log.info("Bỏ nhận xét báo cáo vì có số không đối chiếu được")
            return None
        return note

    async def deliver(self, name: str, user_id: str, text: str) -> None:
        channel, chat_id = user_id.split(":", 1)
        await self.sender(channel).send(chat_id, text)
        # báo cáo thành cuộc trò chuyện mới: người nhận hỏi tiếp trong ngữ cảnh báo cáo
        from langchain_core.messages import AIMessage

        store = self.service.store
        thread = await asyncio.to_thread(store.new_thread, user_id, self.config.reports[name].title)
        graph = await self.service.ensure_ready()
        await graph.aupdate_state({"configurable": {"thread_id": f"{user_id}:{thread}"}},
                                  {"messages": [AIMessage(content=text)]}, as_node="agent")
        store.audit(user_id, "report_sent", report=name, chars=len(text))

    async def send_now(self, name: str, only: list[str] | None = None, today: date | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        for uid in only or self.recipients(self.config.reports[name]):
            try:
                text = await self.build(name, uid, today)
                if text is None:
                    out[uid] = "bỏ qua (không có mục nào được xem)"
                    continue
                await self.deliver(name, uid, text)
                out[uid] = "đã gửi"
            except Exception as e:
                log.error("Gửi báo cáo %s cho %s lỗi: %s", name, uid, e)
                out[uid] = f"lỗi: {e}"
        return out

    # ------------------------------------------------------------------ lịch
    async def tick(self, now: datetime | None = None) -> dict[str, dict[str, str]]:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        store = self.service.store
        results: dict[str, dict[str, str]] = {}
        for name, spec in self.config.reports.items():
            if not spec.enabled:
                continue
            slot = spec.schedule.slot(now.date(), self.tz)
            if slot is None or now < slot:
                continue
            if now - slot > timedelta(minutes=spec.max_delay_minutes):
                continue                                             # bật lại quá muộn: bỏ qua hôm nay
            day = now.date().isoformat()
            pending = []
            for uid in self.recipients(spec):
                st = await asyncio.to_thread(store.kv_get, f"report:{name}:{uid}") or {}
                if st.get("sent") == day or (st.get("day") == day and st.get("attempts", 0) >= 3):
                    continue
                pending.append(uid)
            if not pending:
                continue
            res = await self.send_now(name, pending, now.date())
            for uid, status in res.items():
                key = f"report:{name}:{uid}"
                st = await asyncio.to_thread(store.kv_get, key) or {}
                if status == "đã gửi" or status.startswith("bỏ qua"):
                    st = {"sent": day}
                else:
                    st = {"day": day, "attempts": (st.get("attempts", 0) if st.get("day") == day else 0) + 1,
                          "error": status}
                await asyncio.to_thread(store.kv_set, key, st)
            results[name] = res
        return results

    async def run_forever(self) -> None:
        if not self.config.reports:
            return
        log.info("Lịch báo cáo: %s", list(self.config.reports))
        while True:
            try:
                self.config = await asyncio.to_thread(load_reports, self.ws)    # sửa reports.yaml là có hiệu lực
                await self.tick()
            except Exception:
                log.exception("Vòng báo cáo lỗi")
            await asyncio.sleep(60)
