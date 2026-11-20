"""Cảnh báo theo ngưỡng (alerts.yaml): điều kiện khai báo, quyền theo nguồn, chống gửi lặp, lịch, CLI, /admin."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from bizagent.agent.service import AgentService
from bizagent.alerts import AlertRunner, ConditionError, check_alerts, load_alerts, parse_condition
from bizagent.dashboards.config import DashboardCatalog
from bizagent.sync import build_runner
from bizagent.webauth import hash_password
from bizagent.workspace import UserEntry, WorkspaceError

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, tzinfo=TZ)


class FakeSender:
    def __init__(self, two_way: bool = True, fail: int = 0):
        self.two_way = two_way
        self.fail = fail                            # số lần đầu gửi lỗi
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("mạng lỗi")
        self.sent.append((chat_id, text))


ALERTS = """
alerts:
  doanh_thu_giam:
    title: "Doanh thu giảm"
    dataset: kpi
    period: "hôm qua"
    params: {cua_hang: null}
    condition: "pct_change(doanh_thu, doanh_thu_truoc) <= 1000"
    label: "Doanh thu so với hôm trước"
    check: {at: "09:00"}
    recipients: ["role:admin", "role:manager", "telegram:30", "telegram:99"]
    dashboard: tong_quan
  luon_bao:
    dataset: kpi
    period: "hôm qua"
    params: {cua_hang: null}
    condition: "so_don >= 0"
    check: {every: 30m, between: "08:00-22:00"}
    recipients: ["telegram:10"]
  cua_hang:
    title: "Cửa hàng nhiều đơn"
    dataset: theo_cua_hang
    period: "hôm qua"
    condition: "so_don > 1"
    label: "Số đơn"
    max_rows: 2
    check: {every: 1h}
    cooldown: 3h
    recipients: ["telegram:10"]
  muc_tieu:
    dataset: dat_muc_tieu
    period: "tháng này"
    condition: "ty_le_dat < 1000"
    label_column: cua_hang
    check: {at: "10:00"}
    recipients: ["telegram:31"]
  khong_vuot:
    dataset: kpi
    period: "hôm qua"
    params: {cua_hang: null}
    condition: "doanh_thu < 0"
    format: currency
    recipients: ["telegram:10"]
"""


@pytest.fixture
def alert_env(demo_ws):
    from bizagent.sources.registry import SourceRegistry

    reg = SourceRegistry(demo_ws)
    runner = build_runner(demo_ws, reg)
    for job in runner.config.jobs:
        runner.run_job(job)
    reg.close()
    (demo_ws.path / "alerts.yaml").write_text(ALERTS, encoding="utf-8")
    demo_ws.users += [UserEntry(id="telegram:10", role="manager"), UserEntry(id="zalo:20", role="admin"),
                      UserEntry(id="email:sep@congty.vn", role="admin"), UserEntry(id="telegram:30", role="staff"),
                      UserEntry(id="telegram:31", role="staff")]
    return demo_ws


def make_runner(svc, **senders):
    senders = {"telegram": FakeSender(), "zalo": FakeSender(), "email": FakeSender(two_way=False), **senders}
    return AlertRunner(svc, senders=senders), senders


# ----------------------------------------------------------------------------- điều kiện
def test_parse_condition():
    row = {"doanh_thu": 70, "doanh_thu_truoc": 100, "ton_kho": 3, "ty_le": 20, "trong": None}
    cases = {
        "doanh_thu < 0.8 * doanh_thu_truoc": (True, 70, 80),
        "doanh_thu < 80% * doanh_thu_truoc": (True, 70, 80),
        "doanh_thu_truoc * 0,5 > doanh_thu": (False, 50, 70),
        "pct_change(doanh_thu, doanh_thu_truoc) <= -20": (True, -30, -20),
        "ton_kho < 10": (True, 3, 10),
        "ty_le ≥ 30%": (False, 20, 30),
        "ton_kho = 3": (True, 3, 3),
        "trong < 10": (False, None, 10),                                  # NULL: không xét
        "pct_change(doanh_thu, trong) < 0": (False, None, 0),
    }
    for text, (ok, lv, rv) in cases.items():
        got_ok, got_l, got_r = parse_condition(text).test(row)
        assert got_ok is ok, text
        assert (got_l is None) if lv is None else got_l == pytest.approx(lv), text
        assert got_r == pytest.approx(rv), text
    assert parse_condition("pct_change(a, b) > 5").is_pct
    assert parse_condition("a < 0.8 * b").columns == ["a", "b"]
    for bad in ['__import__("os").system("x") > 1', "a < b < c", "a < 1 and b > 2", "1 < 2", "a < b * c",
                "eval(a, b) > 1", "a.b < 1", "a <", "a < 1; import os", "", "a < -b", "lambda: 1 < a"]:
        with pytest.raises(ConditionError):
            parse_condition(bad)


def test_config_validation(demo_ws):
    p = demo_ws.path / "alerts.yaml"
    for body, msg in [
        ("alerts: {a: {dataset: kpi, condition: 'x <', recipients: []}}", "cần tên cột"),
        ("alerts: {a: {dataset: kpi, condition: 'x < 1', recipients: [], check: {every: 5s}}}", "every"),
        ("alerts: {a: {dataset: kpi, condition: 'x < 1', recipients: [], check: {}}}", "đúng một"),
        ("alerts: {a: {dataset: kpi, condition: 'x < 1', recipients: [], cooldown: mai}}", "cooldown"),
        ("alerts: {'a:b': {dataset: kpi, condition: 'x < 1', recipients: []}}", "tên cảnh báo"),
        ("alerts: {a: {dataset: kpi, condition: 'x < 1', recipients: [], sql: 'DROP'}}", "sql"),
    ]:
        p.write_text(body, encoding="utf-8")
        with pytest.raises(WorkspaceError, match=msg):
            load_alerts(demo_ws)
    p.write_text("alerts:\n  a: {dataset: khong_co, condition: 'x < 1', recipients: ['role:admin'],"
                 " dashboard: nao_do, period: 'mai kia'}\n", encoding="utf-8")
    problems, notes = check_alerts(demo_ws, DashboardCatalog(demo_ws))
    assert any("khong_co" in m for m in problems) and any("nao_do" in m for m in problems)
    assert any("period" in m for m in problems) and any("người nhận" in m for m in notes)


def test_demo_and_template_alerts_are_valid(demo_ws):
    from bizagent.workspace import load_workspace
    from tests.conftest import WORKSPACES

    assert set(load_alerts(demo_ws).alerts) >= {"doanh_thu_giam", "cham_muc_tieu"}
    problems, _ = check_alerts(demo_ws, DashboardCatalog(demo_ws))
    assert problems == []
    assert load_alerts(load_workspace(str(WORKSPACES / "_template"))).alerts == {}


# ----------------------------------------------------------------------------- gửi theo quyền, chống gửi lặp
async def test_alert_permissions_message_and_dedup(alert_env, fake_model_cls):
    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    svc.store.add_grant("telegram:30", "warehouse", "t")                   # staff có kho nhưng không có sales
    rr, s = make_runner(svc)
    res = await rr.check("doanh_thu_giam", at(19, 9, 5))
    assert res.status == {"zalo:20": "đã gửi", "email:sep@congty.vn": "đã gửi", "telegram:10": "đã gửi",
                          "telegram:30": "bỏ qua (không xem được nguồn sales)",
                          "telegram:99": "bỏ qua (chưa được cấp quyền)"}
    assert res.summary() == "vượt ngưỡng, gửi 3/5 người"
    chat_id, text = s["telegram"].sent[0]
    assert chat_id == "10"
    lines = text.splitlines()
    assert lines[0] == "🚨 Cảnh báo: Doanh thu giảm"
    assert lines[1].startswith("Doanh thu so với hôm trước: ") and lines[1].endswith("%")
    assert lines[2] == "Ngưỡng: ≤ 1.000,0%" and lines[3] == "Kỳ dữ liệu: 18/11/2026"
    assert "/d/tong_quan" in lines[4] and lines[-1] == "Nhắn tiếp để hỏi thêm."
    mail = s["email"].sent[0]
    assert mail[0] == "sep@congty.vn" and "Nhắn tiếp" not in mail[1]
    # chat: tin cảnh báo thành cuộc trò chuyện mới để hỏi tiếp
    assert svc.store.list_threads("telegram:10")[0]["title"] == "Cảnh báo: Doanh thu giảm"
    assert svc.store.list_threads("email:sep@congty.vn") == []
    assert sum(r["kind"] == "alert_sent" for r in svc.store.audit_rows()) == 3

    again = await rr.check("doanh_thu_giam", at(19, 15))                     # cùng kỳ: không gửi lại
    assert set(again.status.values()) >= {"bỏ qua (đã gửi trong kỳ này)"} and len(s["telegram"].sent) == 1
    forced = await rr.check("doanh_thu_giam", at(19, 15), force=True, only=["telegram:10"])
    assert forced.status == {"telegram:10": "đã gửi"} and len(s["telegram"].sent) == 2

    nf = await rr.check("khong_vuot", at(19, 9))
    assert nf.status == {"telegram:10": "không vượt ngưỡng"} and nf.result.current.value > 0
    assert nf.summary() == "không vượt ngưỡng" and len(s["telegram"].sent) == 2
    await svc.aclose()


async def test_alert_multi_rows_cooldown_and_warehouse_permission(alert_env, fake_model_cls):
    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    rr, s = make_runner(svc)
    res = await rr.check("cua_hang", at(19, 9))
    text = s["telegram"].sent[0][1]
    assert res.result.multi and len(res.result.matches) == 3
    assert "Số đơn > 1: 3/3 dòng" in text and text.count("• ") == 3 and "và 1 dòng khác" in text
    assert "• Chi nhánh" in text
    assert (await rr.check("cua_hang", at(19, 11))).status["telegram:10"].startswith("bỏ qua (đã gửi lúc 09:00")
    assert (await rr.check("cua_hang", at(19, 12, 1))).status == {"telegram:10": "đã gửi"}       # hết cooldown 3h

    # telegram:31 (staff) chỉ được cấp sales: không nhận cảnh báo của dataset trên kho
    svc.store.add_grant("telegram:31", "sales", "t")
    assert (await rr.check("muc_tieu", at(19, 10))).status == {
        "telegram:31": "bỏ qua (không xem được nguồn warehouse)"}
    svc.store.add_grant("telegram:31", "warehouse", "t")
    assert (await rr.check("muc_tieu", at(19, 10))).status == {"telegram:31": "đã gửi"}
    await svc.aclose()


async def test_alert_schedule_and_retry(alert_env, fake_model_cls):
    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    rr, s = make_runner(svc, telegram=FakeSender(fail=1))
    for name in ("cua_hang", "muc_tieu", "khong_vuot"):
        rr.config.alerts[name].enabled = False

    assert await rr.tick(at(19, 7, 30)) == {}                         # 09:00 chưa tới, 08:00-22:00 chưa tới
    res = await rr.tick(at(19, 8, 0))
    assert list(res) == ["luon_bao"] and res["luon_bao"].status == {"telegram:10": "lỗi: mạng lỗi"}
    assert rr.last_check("luon_bao")["retry"] is True
    res = await rr.tick(at(19, 8, 1))                                  # lần trước lỗi: thử lại sau 1 phút
    assert res["luon_bao"].status == {"telegram:10": "đã gửi"}
    assert await rr.tick(at(19, 8, 20)) == {}                          # chưa đủ 30 phút
    res = await rr.tick(at(19, 8, 31))
    assert res["luon_bao"].status == {"telegram:10": "bỏ qua (đã gửi trong kỳ này)"}

    res = await rr.tick(at(19, 9, 0))
    assert set(res) == {"doanh_thu_giam"}                               # luon_bao: mới kiểm tra lúc 8:31
    assert res["doanh_thu_giam"].status["zalo:20"] == "đã gửi"
    assert "doanh_thu_giam" not in await rr.tick(at(19, 9, 40))       # mỗi ngày một lần
    assert "doanh_thu_giam" not in await rr.tick(at(20, 12, 30))      # quá max_delay_minutes: bỏ qua hôm đó
    assert await rr.tick(at(19, 23, 0)) == {}                          # ngoài khung 08:00-22:00

    res = await rr.tick(at(21, 9, 0))                                   # kỳ mới (20/11): gửi lại
    assert res["doanh_thu_giam"].result.period_key == "2026-11-20..2026-11-20"
    assert res["luon_bao"].status == {"telegram:10": "đã gửi"}
    assert "vượt ngưỡng" in rr.last_check("luon_bao")["summary"]
    await svc.aclose()


async def test_alert_gives_up_after_three_failures(alert_env, fake_model_cls):
    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    rr, s = make_runner(svc, telegram=FakeSender(fail=10))
    for i in range(3):
        assert (await rr.check("luon_bao", at(19, 9, i))).status == {"telegram:10": "lỗi: mạng lỗi"}
    assert (await rr.check("luon_bao", at(19, 9, 5))).status == {"telegram:10": "bỏ qua (gửi lỗi 3 lần trong kỳ này)"}
    s["telegram"].fail = 0
    assert (await rr.check("luon_bao", at(20, 9))).status == {"telegram:10": "đã gửi"}     # kỳ mới
    await svc.aclose()


async def test_alert_dataset_error_and_background_task(alert_env, fake_model_cls, monkeypatch):
    (alert_env.path / "alerts.yaml").write_text(
        "alerts:\n  sai_cot: {dataset: kpi, period: 'hôm qua', params: {cua_hang: null}, condition: 'khong_co > 1',"
        " recipients: ['telegram:10']}\n", encoding="utf-8")
    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    rr, _ = make_runner(svc)
    res = await rr.tick(at(19, 9))
    assert "không có cột khong_co" in res["sai_cot"].error and rr.last_check("sai_cot")["retry"]

    monkeypatch.setenv("BIZAGENT_BACKGROUND_TASKS", "0")
    assert svc.start_background_tasks() == []
    monkeypatch.setenv("BIZAGENT_BACKGROUND_TASKS", "1")
    tasks = svc.start_background_tasks()
    assert svc.alert_runner.config.alerts and any("AlertRunner.run_forever" in str(t.get_coro()) for t in tasks)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await svc.aclose()


# ----------------------------------------------------------------------------- CLI, /admin
def test_cli_alert(alert_env, fake_model_cls, monkeypatch, capsys):
    from bizagent import cli

    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    sent = FakeSender()
    monkeypatch.setattr(cli, "_service", lambda args: svc)
    monkeypatch.setattr(AlertRunner, "sender", lambda self, ch: sent)
    assert cli.main(["alert", "-w", str(alert_env.path)]) == 0
    assert "doanh_thu_giam" in capsys.readouterr().out
    assert cli.main(["alert", "luon_bao", "--preview", "-w", str(alert_env.path)]) == 0
    out = capsys.readouterr().out
    assert "telegram:10: sẽ gửi" in out and "🚨 Cảnh báo: luon_bao" in out and sent.sent == []
    assert cli.main(["alert", "luon_bao", "-w", str(alert_env.path)]) == 0
    assert "telegram:10: đã gửi" in capsys.readouterr().out and len(sent.sent) == 1
    assert cli.main(["alert", "khong_co", "-w", str(alert_env.path)]) == 1


def test_admin_alerts(alert_env, fake_model_cls, monkeypatch):
    from bizagent.channels.rest import create_app

    monkeypatch.setenv("BIZAGENT_SECRET_KEY", "k")
    monkeypatch.delenv("BIZAGENT_API_TOKEN", raising=False)
    monkeypatch.setenv("BIZAGENT_BACKGROUND_TASKS", "0")
    pw = hash_password("matkhau123")
    alert_env.users += [UserEntry(id="web:boss", role="admin", password_hash=pw),
                        UserEntry(id="web:lan", role="manager", password_hash=pw)]
    svc = AgentService(alert_env, model=fake_model_cls(responses=[], seen=[]))
    sent = FakeSender()
    monkeypatch.setattr(AlertRunner, "sender", lambda self, ch: sent)
    app = create_app(svc)
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://testserver"}

    def login(c, user):
        c.cookies.clear()
        c.post("/login", content=f"username={user}&password=matkhau123&next=/admin", headers=form,
               follow_redirects=False)

    with TestClient(app) as c:
        login(c, "boss")
        page = c.get("/admin").text
        assert "Cảnh báo theo ngưỡng" in page and "luon_bao" in page and "chưa kiểm tra" in page
        assert "/admin/alerts/luon_bao/check" in page
        r = c.post("/admin/alerts/luon_bao/check", headers=form, follow_redirects=False)
        assert r.status_code == 303 and "canh-bao" in r.headers["location"] and len(sent.sent) == 1
        assert "vượt ngưỡng" in c.get(r.headers["location"]).text
        c.post("/admin/alerts/luon_bao/check", headers=form)                  # cùng kỳ: không gửi lại
        assert len(sent.sent) == 1
        assert c.post("/admin/alerts/khong_co/check", headers=form).status_code == 404
        evil = {**form, "Origin": "https://evil.example"}
        assert c.post("/admin/alerts/luon_bao/check", headers=evil).status_code == 403
        assert any(r["kind"] == "alert_manual" for r in svc.store.audit_rows())

        login(c, "lan")                                                   # manager: không kiểm tra cảnh báo
        assert "Cảnh báo theo ngưỡng" not in c.get("/admin").text
        assert c.post("/admin/alerts/luon_bao/check", headers=form).status_code == 403
