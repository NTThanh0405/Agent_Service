"""Báo cáo định kỳ gửi qua Telegram, Zalo, Slack và email — workspaces/<ten>/reports.yaml.

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
import html
import logging
import os
import re
from dataclasses import dataclass, field
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
REPORT_CHANNELS = ("telegram", "zalo", "slack", "email")     # kênh gửi được báo cáo (tiền tố ID người nhận)
DAYS =["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


# ----------------------------------------------------------------------------- nội dung báo cáo
@dataclass
class SectionOut:
    title: str
    kpis: list[tuple[str, str, str]] = field(default_factory=list)   # (nhãn, giá trị, thay đổi so kỳ trước)
    columns: list[str] = field(default_factory=list)                 # tiêu đề cột của bảng (nếu có bảng)
    rows: list[list[str]] = field(default_factory=list)              # các ô đã định dạng
    has_table: bool = False
    error: str | None = None                                         # mục tạm thiếu dữ liệu


@dataclass
class ReportOut:
    """Một báo cáo đã dựng xong cho một nhóm người nhận: xuất ra chữ (chat) hoặc HTML (email)."""

    title: str
    label: str
    sections: list[SectionOut]
    commentary: str | None = None
    dashboard_url: str | None = None

    @property
    def subject(self) -> str:
        return f"{self.title} — {self.label}"

    def body_text(self) -> str:
        """Phần số liệu (không gồm nhận xét/link): đưa cho AI viết nhận xét."""
        blocks = []
        for s in self.sections:
            if s.error is not None:
                blocks.append(f"▪ {s.title}\n  (tạm thiếu dữ liệu: {s.error})")
                continue
            lines = [f"▪ {s.title}"]
            lines += [f"  {label}: {value}{chg}" for label, value, chg in s.kpis]
            if s.has_table:
                lines += [f"  {i}. " + " · ".join(r) for i, r in enumerate(s.rows, 1)]
                if not s.rows:
                    lines.append("  (không có dữ liệu)")
            blocks.append("\n".join(lines))
        return f"📊 {self.subject}\n\n" + "\n\n".join(blocks)

    def text(self, followup: bool = True) -> str:
        text = self.body_text()
        if self.commentary:
            text += f"\n\n💬 {self.commentary}"
        if self.dashboard_url:
            text += f"\n\n🔗 Dashboard: {self.dashboard_url}"
        return text + ("\n\nNhắn tiếp để hỏi thêm về báo cáo này." if followup else "")

    def html(self, workspace_name: str = "") -> str:
        """Email HTML: bảng thật, CSS nội tuyến (trình đọc mail bỏ <style>), mọi giá trị đã thoát ký tự."""
        e = html.escape
        td = "padding:6px 10px;border-bottom:1px solid #e5e7eb;"
        parts = [f'<h2 style="margin:0 0 4px;font-size:20px">{e(self.title)}</h2>'
                 f'<div style="color:#6b7280;margin-bottom:18px">{e(self.label)}</div>']
        for s in self.sections:
            parts.append(f'<h3 style="margin:20px 0 8px;font-size:16px">{e(s.title)}</h3>')
            if s.error is not None:
                parts.append(f'<p style="color:#b45309">Tạm thiếu dữ liệu: {e(s.error)}</p>')
                continue
            if s.kpis:
                cells = "".join(
                    f'<td style="padding:10px 14px;border:1px solid #e5e7eb;border-radius:6px">'
                    f'<div style="color:#6b7280;font-size:12px">{e(label)}</div>'
                    f'<div style="font-size:18px;font-weight:600">{e(value)}</div>'
                    f'<div style="font-size:12px;color:{"#047857" if "▲" in chg else "#b91c1c"}">{e(chg.strip())}</div>'
                    "</td>" for label, value, chg in s.kpis)
                parts.append(f'<table cellspacing="8" style="border-collapse:separate"><tr>{cells}</tr></table>')
            if s.has_table:
                if not s.rows:
                    parts.append('<p style="color:#6b7280">(không có dữ liệu)</p>')
                    continue
                head = "".join(f'<th style="{td}text-align:left;background:#f3f4f6">{e(c)}</th>' for c in s.columns)
                body = "".join("<tr>" + "".join(f'<td style="{td}">{e(c)}</td>' for c in r) + "</tr>" for r in s.rows)
                parts.append(f'<table style="border-collapse:collapse;min-width:60%"><tr>{head}</tr>{body}</table>')
        if self.commentary:
            parts.append(f'<p style="margin-top:20px;padding:10px 14px;background:#eff6ff;border-radius:6px">'
                         f'💬 {e(self.commentary)}</p>')
        if self.dashboard_url:
            parts.append(f'<p><a href="{e(self.dashboard_url)}">Mở dashboard →</a></p>')
        foot = f"Báo cáo tự động từ {e(workspace_name)}." if workspace_name else "Báo cáo tự động."
        parts.append(f'<p style="color:#9ca3af;font-size:12px;margin-top:24px">{foot} Đừng trả lời email này.</p>')
        return ('<!doctype html><html><body style="font-family:Segoe UI,Arial,sans-serif;color:#111827;'
                f'max-width:760px;margin:0 auto;padding:16px">{"".join(parts)}</body></html>')


# ----------------------------------------------------------------------------- người gửi
class Sender:
    #: True: người nhận nhắn lại được (báo cáo thành cuộc trò chuyện để hỏi tiếp). Email: không.
    two_way: bool = True

    async def send(self, chat_id: str, text: str) -> None:  # pragma: no cover - giao diện
        raise NotImplementedError

    async def send_report(self, chat_id: str, report: ReportOut, workspace_name: str = "") -> str:
        """Gửi báo cáo; trả về nội dung chữ đã gửi (lưu làm ngữ cảnh hỏi tiếp)."""
        text = report.text(followup=self.two_way)
        await self.send(chat_id, text)
        return text


EMAIL_ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def smtp_settings() -> dict[str, Any]:
    """Cấu hình SMTP từ biến môi trường (.env). Ném RuntimeError nếu thiếu."""
    missing = [n for n in ("SMTP_HOST", "SMTP_FROM") if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"Gửi email cần đặt {', '.join(missing)} trong .env")
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    if security not in ("starttls", "ssl", "none"):
        raise RuntimeError("SMTP_SECURITY phải là starttls | ssl | none")
    default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
    return {
        "host": os.environ["SMTP_HOST"],
        "port": int(os.environ.get("SMTP_PORT") or default_port),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": os.environ["SMTP_FROM"],
        "security": security,
        "timeout": float(os.environ.get("SMTP_TIMEOUT", "30")),
    }


def smtp_missing_env() -> list[str]:
    return [n for n in ("SMTP_HOST", "SMTP_FROM") if not os.environ.get(n)]


class EmailSender(Sender):
    """Gửi báo cáo qua SMTP (Gmail/Google Workspace, Microsoft 365, máy chủ mail nội bộ...).
    Email gồm bản HTML (bảng) và bản chữ thường cho trình đọc mail không hiện HTML."""

    two_way = False

    def __init__(self, smtp_factory=None):
        self._smtp_factory = smtp_factory          # test thay bằng SMTP giả

    async def send(self, chat_id: str, text: str) -> None:
        await asyncio.to_thread(self._send, chat_id, text.splitlines()[0] if text else "Báo cáo", text, None)

    async def send_report(self, chat_id: str, report: ReportOut, workspace_name: str = "") -> str:
        text = report.text(followup=False)
        await asyncio.to_thread(self._send, chat_id, report.subject, text, report.html(workspace_name))
        return text

    def _send(self, to: str, subject: str, text: str, html_body: str | None) -> None:
        import smtplib
        import ssl
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid

        if not EMAIL_ADDRESS.match(to):
            raise RuntimeError(f"Địa chỉ email không hợp lệ: {to}")
        cfg = smtp_settings()
        msg = EmailMessage()
        msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")
        msg["From"] = cfg["sender"]
        msg["To"] = to
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=cfg["sender"].rpartition("@")[2].strip(">") or None)
        msg["Auto-Submitted"] = "auto-generated"           # báo cho máy chủ mail: không gửi trả lời tự động
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        factory = self._smtp_factory
        context = ssl.create_default_context()
        if factory is None:
            factory = smtplib.SMTP_SSL if cfg["security"] == "ssl" else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": cfg["timeout"]}
        if cfg["security"] == "ssl":
            kwargs["context"] = context
        try:
            with factory(cfg["host"], cfg["port"], **kwargs) as smtp:
                if cfg["security"] == "starttls":
                    smtp.starttls(context=context)
                if cfg["user"]:
                    smtp.login(cfg["user"], cfg["password"])
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError("Máy chủ mail từ chối đăng nhập (kiểm tra SMTP_USER / SMTP_PASSWORD; "
                               "Gmail cần App Password)") from e
        except (smtplib.SMTPException, OSError) as e:
            raise RuntimeError(f"Gửi email lỗi: {e.__class__.__name__}: {e}") from e


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


# ----------------------------------------------------------------------------- phần dùng chung (báo cáo, cảnh báo)
def make_sender(service, channel: str, cache: dict[str, Sender]) -> Sender:
    """Sender của một kênh (tạo một lần, giữ trong `cache`)."""
    if channel not in cache:
        factories = {"telegram": TelegramSender, "zalo": lambda: ZaloSender(service.store),
                     "slack": SlackSender, "email": EmailSender}
        if channel not in factories:
            raise RuntimeError(f"Kênh '{channel}' không gửi được báo cáo (dùng telegram/zalo/slack/email)")
        cache[channel] = factories[channel]()
    return cache[channel]


def expand_recipients(ws, recipients: list[str]) -> list[str]:
    """`role:<role>` -> mọi người dùng có role đó; chỉ giữ ID thuộc kênh gửi được, bỏ trùng."""
    out: list[str] = []
    for r in recipients:
        if r.startswith("role:"):
            out += [u.id for u in ws.all_users() if u.role == r[5:]]
        else:
            out.append(r)
    return [r for r in dict.fromkeys(out) if r.split(":", 1)[0] in REPORT_CHANNELS]


def period_params(ws, period: str, params: dict[str, Any], today: date) -> tuple[dict[str, Any], str, str]:
    """(tham số cho dataset, nhãn kỳ để hiển thị, khoá kỳ "start..end")."""
    p = resolve_period(period, today, ws.info.week_start)
    prev = p.previous()
    label = p.start.strftime("%d/%m/%Y") if p.days == 1 else f"{p.start:%d/%m} – {p.end:%d/%m/%Y}"
    return ({**params, "start": p.start.isoformat(), "end": p.end.isoformat(),
             "prev_start": prev.start.isoformat(), "prev_end": prev.end.isoformat()},
            label, f"{p.start.isoformat()}..{p.end.isoformat()}")


async def save_as_thread(service, user_id: str, title: str, text: str) -> None:
    """Tin đã gửi thành cuộc trò chuyện mới của người nhận: nhắn tiếp là hỏi trong ngữ cảnh tin đó."""
    from langchain_core.messages import AIMessage

    thread = await asyncio.to_thread(service.store.new_thread, user_id, title)
    graph = await service.ensure_ready()
    await graph.aupdate_state({"configurable": {"thread_id": f"{user_id}:{thread}"}},
                              {"messages": [AIMessage(content=text)]}, as_node="agent")


# ----------------------------------------------------------------------------- dựng và gửi
class ReportRunner:
    def __init__(self, service, senders: dict[str, Sender] | None = None):
        self.service = service
        self.ws = service.ws
        self.tz = ZoneInfo(self.ws.info.timezone)
        self.config = load_reports(self.ws)
        self._senders = senders or {}

    def sender(self, channel: str) -> Sender:
        return make_sender(self.service, channel, self._senders)

    def recipients(self, spec: ReportSpec) -> list[str]:
        return expand_recipients(self.ws, spec.recipients)

    def params(self, spec: ReportSpec, today: date) -> tuple[dict[str, Any], str]:
        params, label, _ = period_params(self.ws, spec.period, spec.params, today)
        return params, label

    async def build(self, name: str, user_id: str, today: date | None = None) -> str | None:
        """Nội dung chữ của báo cáo cho một người nhận (None nếu người đó không được xem mục nào)."""
        report = await self.build_report(name, user_id, today)
        if report is None:
            return None
        two_way = user_id.split(":", 1)[0] != "email"
        return report.text(followup=two_way)

    async def build_report(self, name: str, user_id: str, today: date | None = None) -> ReportOut | None:
        """Báo cáo có cấu trúc cho một người nhận (None nếu người đó không được xem mục nào)."""
        spec = self.config.reports[name]
        role = self.ws.role_of(user_id)
        if role is None:
            return None
        engine = self.service.dashboard_engine
        if engine is None:
            raise WorkspaceError("Báo cáo cần datasets.yaml (dashboards bị lỗi cấu hình)")
        today = today or datetime.now(self.tz).date()
        params, label = self.params(spec, today)
        sections: list[SectionOut] = []
        data_texts: list[str] = []
        for sec in spec.sections:
            ds = engine.catalog.dataset(sec.dataset)
            if ds.source not in self.ws.sources_for(user_id, role):
                continue
            try:
                r = await asyncio.to_thread(engine.run_dataset, sec.dataset, params, role, user_id=user_id,
                                            limit=max(50, sec.table.max_rows if sec.table else 1), use_cache=False)
            except (SourceError, DashboardError) as e:
                sections.append(SectionOut(sec.title, error=str(e)))
                continue
            data_texts.append(r.to_text())
            out = SectionOut(sec.title)
            row = dict(zip(r.columns, r.rows[0], strict=True)) if r.rows else {}
            for k in sec.kpis:
                cmp = change(row.get(k.value), row.get(k.compare)) if k.compare else ""
                out.kpis.append((k.label, fmt(row.get(k.value), k.format), cmp))
            if sec.table:
                out.has_table = True
                cols = sec.table.columns or r.columns
                idx = [r.columns.index(c) for c in cols if c in r.columns]
                out.columns = [r.columns[j] for j in idx]
                out.rows = [[fmt(rr[j], sec.table.formats.get(r.columns[j], "number")) for j in idx]
                            for rr in r.rows[: sec.table.max_rows]]
            sections.append(out)
        if not sections:
            return None
        report = ReportOut(spec.title, label, sections)
        if spec.commentary:
            report.commentary = await self.commentary(report.body_text(), data_texts)
        cat = self.service.catalog
        if spec.dashboard and cat and cat.config.public_url and cat.can_view(spec.dashboard, role, user_id):
            report.dashboard_url = cat.link(spec.dashboard)
        return report

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

    async def deliver(self, name: str, user_id: str, report: ReportOut) -> None:
        channel, chat_id = user_id.split(":", 1)
        sender = self.sender(channel)
        two_way = getattr(sender, "two_way", True)
        if hasattr(sender, "send_report"):
            text = await sender.send_report(chat_id, report, self.ws.info.display_name or self.ws.name)
        else:                                       # sender tự viết chỉ có send(chat_id, text)
            text = report.text(followup=two_way)
            await sender.send(chat_id, text)
        store = self.service.store
        if two_way:
            # báo cáo thành cuộc trò chuyện mới: người nhận hỏi tiếp trong ngữ cảnh báo cáo
            await save_as_thread(self.service, user_id, self.config.reports[name].title, text)
        await asyncio.to_thread(store.audit, user_id, "report_sent", report=name, channel=channel, chars=len(text))

    def _audience(self, user_id: str) -> tuple | None:
        """Hai người cùng role và cùng tập nguồn được xem thì nhận đúng một nội dung báo cáo."""
        role = self.ws.role_of(user_id)
        return None if role is None else (role, tuple(sorted(self.ws.sources_for(user_id, role))))

    async def send_now(self, name: str, only: list[str] | None = None, today: date | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        # Dựng báo cáo một lần cho mỗi nhóm quyền (truy vấn + nhận xét AI), không phải một lần cho mỗi người nhận
        built: dict[tuple, ReportOut | None] = {}
        for uid in only or self.recipients(self.config.reports[name]):
            try:
                group = self._audience(uid)
                if group is None or group not in built:
                    report = await self.build_report(name, uid, today)
                    if group is not None:
                        built[group] = report
                else:
                    report = built[group]
                if report is None:
                    out[uid] = "bỏ qua (không có mục nào được xem)"
                    continue
                await self.deliver(name, uid, report)
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
