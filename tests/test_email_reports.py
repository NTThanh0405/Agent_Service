"""Báo cáo định kỳ qua email (SMTP)."""

from datetime import date

import pytest

from bizagent.access import AccessError, normalize_user_id
from bizagent.agent.service import AgentService
from bizagent.reports import EmailSender, ReportOut, ReportRunner, SectionOut
from bizagent.workspace import UserEntry


class FakeSMTP:
    """Thay smtplib.SMTP: ghi lại các bước và thư đã gửi."""

    sent: list = []
    calls: list = []

    def __init__(self, host, port, **kwargs):
        self.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def smtp_env(monkeypatch):
    FakeSMTP.sent, FakeSMTP.calls = [], []
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "Bao cao <baocao@example.com>")
    monkeypatch.setenv("SMTP_USER", "baocao@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    return FakeSMTP


@pytest.fixture
def report_ws(demo_ws):
    from bizagent.sources.registry import SourceRegistry
    from bizagent.sync import build_runner

    reg = SourceRegistry(demo_ws)
    runner = build_runner(demo_ws, reg)
    for job in runner.config.jobs:
        runner.run_job(job)
    reg.close()
    return demo_ws


def test_email_user_ids():
    assert normalize_user_id("email:Lan.Nguyen+bc@congty.vn", "web:admin") == "email:Lan.Nguyen+bc@congty.vn"
    for bad in ("email:khong-co-a-cong", "email:a@b", "email:a@b.c\nBcc: x@y.z"):
        with pytest.raises(AccessError):
            normalize_user_id(bad, "web:admin")


async def test_report_sent_by_email_with_html_tables(report_ws, fake_model_cls, smtp_env):
    report_ws.users += [UserEntry(id="email:giamdoc@congty.vn", role="admin"),
                        UserEntry(id="email:quanly@congty.vn", role="manager")]
    svc = AgentService(report_ws, model=fake_model_cls(responses=[], seen=[]))
    rr = ReportRunner(svc, senders={"email": EmailSender(smtp_factory=FakeSMTP)})
    rr.config.reports["bao_cao_sang"].commentary = False
    assert "email:giamdoc@congty.vn" in rr.recipients(rr.config.reports["bao_cao_sang"])     # qua role:admin

    res = await rr.send_now("bao_cao_sang", ["email:giamdoc@congty.vn", "email:quanly@congty.vn"],
                            today=date(2026, 11, 19))
    assert res == {"email:giamdoc@congty.vn": "đã gửi", "email:quanly@congty.vn": "đã gửi"}
    assert ("starttls",) in FakeSMTP.calls and ("login", "baocao@example.com") in FakeSMTP.calls

    msg = FakeSMTP.sent[0]
    assert msg["To"] == "giamdoc@congty.vn" and msg["Subject"] == "Báo cáo sáng — 18/11/2026"
    assert msg["Auto-Submitted"] == "auto-generated"
    text = msg.get_body(("plain",)).get_content()
    html_body = msg.get_body(("html",)).get_content()
    assert "Kết quả hôm qua" in text and "Nhắn tiếp" not in text          # email không trả lời được
    assert "<table" in html_body and "<th" in html_body and "Theo cửa hàng" in html_body
    assert "/d/tong_quan" in html_body                                      # link dashboard

    # email không tạo cuộc trò chuyện hỏi tiếp, nhưng vẫn ghi audit
    assert svc.store.list_threads("email:giamdoc@congty.vn") == []
    assert any(r["kind"] == "report_sent" and r["detail"]["channel"] == "email" for r in svc.store.audit_rows())
    await svc.aclose()


def test_html_escapes_values(smtp_env):
    rep = ReportOut("Báo cáo <b>", "18/11/2026", [
        SectionOut("Bảng", columns=["ten"], rows=[["<script>alert(1)</script>"]], has_table=True)])
    out = rep.html()
    assert "<script>" not in out and "&lt;script&gt;" in out and "Báo cáo &lt;b&gt;" in out


async def test_missing_smtp_config_is_reported(report_ws, fake_model_cls, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    report_ws.users.append(UserEntry(id="email:giamdoc@congty.vn", role="admin"))
    svc = AgentService(report_ws, model=fake_model_cls(responses=[], seen=[]))
    rr = ReportRunner(svc)
    rr.config.reports["bao_cao_sang"].commentary = False
    res = await rr.send_now("bao_cao_sang", ["email:giamdoc@congty.vn"], today=date(2026, 11, 19))
    assert "SMTP_HOST" in res["email:giamdoc@congty.vn"]
    await svc.aclose()


async def test_ssl_mode_and_no_login(smtp_env, monkeypatch):
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.delenv("SMTP_USER")
    rep = ReportOut("BC", "1/1", [SectionOut("A", kpis=[("Doanh thu", "1,2 tỷ", " (▲ 5,0%)")])])
    await EmailSender(smtp_factory=FakeSMTP).send_report("a@b.vn", rep)
    assert FakeSMTP.calls == [("connect", "smtp.example.com", 465)]          # cổng 465, không STARTTLS, không login
    assert "1,2 tỷ" in FakeSMTP.sent[0].get_body(("html",)).get_content()
