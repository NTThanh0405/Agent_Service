"""Giai đoạn 5: đối chiếu số, reviewer, người dùng + quyền cấp riêng, báo cáo định kỳ, Slack, trang /admin."""

import hashlib
import hmac
import json
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from bizagent import access
from bizagent.agent.service import AgentService
from bizagent.channels.common import handle_command
from bizagent.channels.slack import SlackChannel, slack_signature
from bizagent.numcheck import extract_mentions, unverified
from bizagent.reports import ReportRunner, fmt
from bizagent.sync import build_runner
from bizagent.webauth import hash_password
from bizagent.workspace import UserEntry

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def call(name, args, id_="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_}])


# ----------------------------------------------------------------------------- đối chiếu số
def test_numcheck_formats():
    data = ["doanh_thu | truoc | so_don\n41480000 | 37041000 | 1249"]
    good = ("Ngày 18/11/2026 lúc 07:00, năm 2026: doanh thu 41,5 triệu (41.480.000 ₫, 41,480,000), "
            "tăng 12% (11,98%), chênh 4,4 triệu, 1.249 đơn, AOV 33.211 ₫, 41.5tr, 0,04 tỷ")
    assert unverified(good, data) == []
    assert unverified("doanh thu 52 triệu, giảm 7%", data) == ["52 triệu", "7%"]
    assert [m.raw for m in extract_mentions("có 5 cửa hàng, ngày 2026-11-18")] == []     # số nhỏ, ngày


async def test_reviewer_flags_wrong_numbers(demo_ws, fake_model_cls):
    sql = "SELECT COUNT(*) AS so_don FROM orders WHERE order_date = '2026-11-18' AND status = 'completed'"
    model = fake_model_cls(responses=[
        call("run_sql", {"source": "sales", "sql": sql}),
        AIMessage(content="Hôm qua có 9.999 đơn."),
        call("run_sql", {"source": "sales", "sql": sql}, "c2"),
        AIMessage(content="PLACEHOLDER"),
    ], seen=[])
    svc = AgentService(demo_ws, model=model)
    bad = await svc.ask("hôm qua bao nhiêu đơn?", user_id="t:1", role="admin", thread_id="a")
    assert "⚠️ Lưu ý" in bad and "9.999" in bad
    real = int(model.seen[1][-1].content.split("\n")[1])
    model.responses[-1] = AIMessage(content=f"Hôm qua có {real:,} đơn.".replace(",", "."))
    good = await svc.ask("hôm qua bao nhiêu đơn?", user_id="t:1", role="admin", thread_id="b")
    assert "Lưu ý" not in good
    assert any(r["kind"] == "review_flag" for r in svc.store.audit_rows())
    await svc.aclose()


# ----------------------------------------------------------------------------- người dùng + quyền
async def test_users_and_grants(demo_ws, fake_model_cls):
    demo_ws.users.append(UserEntry(id="telegram:1", role="admin"))
    demo_ws.users.append(UserEntry(id="telegram:2", role="manager"))
    model = fake_model_cls(responses=[call("run_sql", {"source": "sales", "sql": "SELECT 1 AS x FROM orders LIMIT 1"}),
                                      AIMessage(content="ok")], seen=[])
    svc = AgentService(demo_ws, model=model)
    ws, st = svc.ws, svc.store
    mgr = ("telegram:2", "manager")

    out = await handle_command(svc, "telegram:1", "admin", "/adduser 555 staff Kế toán")
    assert "telegram:555" in out and ws.role_of("telegram:555") == "staff"
    assert "users.yaml" in await handle_command(svc, "telegram:1", "admin", "/adduser 2 staff")   # người trong file
    assert "quản trị" in await handle_command(svc, "telegram:2", "manager", "/adduser 777 staff")
    assert ws.sources_for("telegram:555", "staff") == []

    with pytest.raises(access.AccessError, match="chính mình"):          # manager không cấp nguồn mình không xem
        access.grant(ws, st, mgr, "telegram:555", "ke_hoach")
    uid, expires = access.grant(ws, st, mgr, "telegram:555", "sales", 48)
    assert uid == "telegram:555" and expires is not None
    assert ws.sources_for("telegram:555", "staff") == ["sales"]
    assert "Cấp riêng" in await handle_command(svc, "telegram:555", "staff", "/quyen")
    assert "chính mình" in await handle_command(svc, "telegram:555", "staff", "/quyen 2")

    await svc.ask("đơn?", user_id="telegram:555", role="staff", thread_id="a")          # tool chạy được nhờ quyền cấp
    assert model.seen[-1][-1].content.startswith("x\n")
    assert "sales" in str(model.seen[0][0].content)                                      # prompt liệt kê nguồn được cấp

    st.add_grant("telegram:555", "warehouse", "test", hours=-1)                         # quyền đã hết hạn
    assert "warehouse" not in ws.sources_for("telegram:555", "staff")
    assert "Đã thu hồi 1" in await handle_command(svc, "telegram:1", "admin", "/thuhoi 555 sales")
    assert ws.sources_for("telegram:555", "staff") == []
    assert "Đã xoá" in await handle_command(svc, "telegram:1", "admin", "/removeuser 555")
    assert ws.role_of("telegram:555") is None
    kinds = {r["kind"] for r in st.audit_rows()}
    assert {"user_add", "grant", "revoke", "user_remove"} <= kinds
    await svc.aclose()


# ----------------------------------------------------------------------------- báo cáo
class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.fixture
def report_env(demo_ws, fake_model_cls):
    from bizagent.sources.registry import SourceRegistry

    reg = SourceRegistry(demo_ws)
    runner = build_runner(demo_ws, reg)
    for job in runner.config.jobs:
        runner.run_job(job)
    reg.close()
    demo_ws.users += [UserEntry(id="telegram:10", role="manager"), UserEntry(id="zalo:20", role="admin")]
    return demo_ws


async def test_report_build_permissions_and_followup(report_env, fake_model_cls):
    model = fake_model_cls(responses=[AIMessage(content="Nhận xét cho người 30."),     # build cho telegram:30
                                      AIMessage(content="Doanh thu đạt 77,7 tỷ."),    # nhận xét có số bịa -> bỏ
                                      AIMessage(content="Đã rõ.")], seen=[])
    svc = AgentService(report_env, model=model)
    tg, zl = FakeSender(), FakeSender()
    rr = ReportRunner(svc, senders={"telegram": tg, "zalo": zl})
    assert rr.recipients(rr.config.reports["bao_cao_sang"]) == ["zalo:20", "telegram:10"]

    svc.store.upsert_user("telegram:30", "staff")
    svc.store.add_grant("telegram:30", "sales", "t")
    text30 = await rr.build("bao_cao_sang", "telegram:30", today=date(2026, 11, 19))
    assert "Kết quả hôm qua" in text30 and "Tiến độ mục tiêu" not in text30           # không có quyền warehouse
    svc.store.revoke_grant("telegram:30")
    assert await rr.build("bao_cao_sang", "telegram:30", today=date(2026, 11, 19)) is None

    res = await rr.send_now("bao_cao_sang", ["telegram:10"], today=date(2026, 11, 19))
    assert res == {"telegram:10": "đã gửi"}
    text = tg.sent[0][1]
    assert tg.sent[0][0] == "10" and "Báo cáo sáng — 18/11/2026" in text and "Tiến độ mục tiêu" in text
    assert "💬" not in text and "/d/tong_quan" in text

    # hỏi tiếp: báo cáo là tin đầu của cuộc trò chuyện mới
    thread = svc.store.current_thread("telegram:10")
    await svc.ask("vì sao giảm?", user_id="telegram:10", role="manager", thread_id=thread)
    msgs = model.seen[-1]
    assert isinstance(msgs[1], AIMessage) and "Báo cáo sáng" in msgs[1].content
    assert isinstance(msgs[2], HumanMessage)
    await svc.aclose()


async def test_report_commentary_kept_when_numbers_match(report_env, fake_model_cls):
    svc = AgentService(report_env, model=fake_model_cls(responses=[], seen=[]))
    rr = ReportRunner(svc, senders={"telegram": FakeSender(), "zalo": FakeSender()})
    base = await rr.build("bao_cao_sang", "telegram:10", today=date(2026, 11, 19))
    so_don = int(base.split("Số đơn: ")[1].split(" ")[0].replace(".", ""))
    svc._model.responses = [AIMessage(content=f"Hôm qua chuỗi có {so_don} đơn hoàn thành.")]
    text = await rr.build("bao_cao_sang", "telegram:10", today=date(2026, 11, 19))
    assert f"💬 Hôm qua chuỗi có {so_don} đơn" in text
    await svc.aclose()


async def test_report_schedule(report_env, fake_model_cls):
    svc = AgentService(report_env, model=fake_model_cls(responses=[AIMessage(content="x")] * 10, seen=[]))
    tg, zl = FakeSender(), FakeSender()
    rr = ReportRunner(svc, senders={"telegram": tg, "zalo": zl})
    rr.config.reports["bao_cao_sang"].commentary = False
    assert await rr.tick(datetime(2026, 11, 19, 6, 59, tzinfo=TZ)) == {}                 # chưa tới giờ
    res = await rr.tick(datetime(2026, 11, 19, 7, 1, tzinfo=TZ))
    assert set(res["bao_cao_sang"]) == {"telegram:10", "zalo:20"}
    assert "bao_cao_tuan" not in res                                                       # 19/11 là thứ Năm
    assert await rr.tick(datetime(2026, 11, 19, 7, 2, tzinfo=TZ)) == {}                  # không gửi trùng
    assert await rr.tick(datetime(2026, 11, 20, 11, 0, tzinfo=TZ)) == {}                 # quá max_delay: bỏ qua
    res = await rr.tick(datetime(2026, 11, 23, 8, 5, tzinfo=TZ))                          # thứ Hai
    assert set(res) == {"bao_cao_sang", "bao_cao_tuan"} and list(res["bao_cao_tuan"]) == ["zalo:20"]
    assert fmt(41480000, "currency") == "41,5 triệu" and fmt(12.345, "percent") == "12,3%"
    await svc.aclose()


# ----------------------------------------------------------------------------- Slack
class FakeSlack:
    def __init__(self):
        self.sent = []

    async def post_message(self, channel, text):
        self.sent.append((channel, text))

    async def aclose(self):
        pass


def test_slack_events(demo_ws, fake_model_cls, monkeypatch):
    from bizagent.channels.rest import create_app

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sss")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    (demo_ws.path / "channels.yaml").write_text("slack: {enabled: true}\n", encoding="utf-8")
    demo_ws.users.append(UserEntry(id="slack:U1", role="manager"))
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[AIMessage(content="Chào")], seen=[]))
    app = create_app(svc)

    def signed(payload, ts=None):
        body = json.dumps(payload).encode()
        ts = ts or str(int(time.time()))
        return body, {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": slack_signature("sss", ts, body),
                      "Content-Type": "application/json"}

    with TestClient(app) as c:
        fake = FakeSlack()
        app.state.slack.client = fake
        body, h = signed({"type": "url_verification", "challenge": "abc"})
        assert c.post("/slack/events", content=body, headers=h).json() == {"challenge": "abc"}
        assert c.post("/slack/events", content=body, headers={**h, "X-Slack-Signature": "v0=sai"}).status_code == 401
        old_body, old_h = signed({"type": "url_verification", "challenge": "x"}, ts=str(int(time.time()) - 600))
        assert c.post("/slack/events", content=old_body, headers=old_h).status_code == 401      # chống phát lại
        ev = {"type": "event_callback", "event_id": "E1",
              "event": {"type": "message", "channel_type": "im", "user": "U1", "text": "chào", "channel": "D1"}}
        body, h = signed(ev)
        assert c.post("/slack/events", content=body, headers=h).status_code == 200
        c.post("/slack/events", content=body, headers=h)                                        # trùng event_id
        bot = {**ev, "event_id": "E2", "event": {**ev["event"], "bot_id": "B1"}}
        b2, h2 = signed(bot)
        c.post("/slack/events", content=b2, headers=h2)                                         # tin của bot: bỏ
        for _ in range(100):
            if fake.sent:
                break
            c.get("/api/v1/health")
        assert fake.sent == [("D1", "Chào")]
        assert c.get("/api/v1/health").json()["channels"]["slack"] == {"enabled": True, "missing_env": []}


def test_slack_signature_formula():
    want = "v0=" + hmac.new(b"s", b"v0:1:{}", hashlib.sha256).hexdigest()
    assert slack_signature("s", "1", b"{}") == want


def test_slack_missing_env(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "x")
    assert SlackChannel.missing_env() == ["SLACK_BOT_TOKEN"]


# ----------------------------------------------------------------------------- trang quản trị
def test_admin_page(report_env, fake_model_cls, monkeypatch):
    from bizagent.channels.rest import create_app

    monkeypatch.setenv("BIZAGENT_SECRET_KEY", "k")
    monkeypatch.delenv("BIZAGENT_API_TOKEN", raising=False)
    pw = hash_password("matkhau123")
    report_env.users += [UserEntry(id="web:boss", role="admin", password_hash=pw),
                         UserEntry(id="web:lan", role="manager", password_hash=pw),
                         UserEntry(id="web:nv", role="staff", password_hash=pw)]
    svc = AgentService(report_env, model=fake_model_cls(responses=[], seen=[]))
    app = create_app(svc)
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://testserver"}

    def login(c, user):
        c.cookies.clear()
        c.post("/login", content=f"username={user}&password=matkhau123&next=/admin", headers=form,
               follow_redirects=False)

    with TestClient(app) as c:
        assert c.get("/admin", follow_redirects=False).status_code == 303
        login(c, "boss")
        page = c.get("/admin").text
        assert "Nguồn dữ liệu" in page and "doanh_thu_ngay" in page and "bao_cao_sang" in page
        r = c.post("/admin/users/add", content="id=telegram%3A888&name=An&role=staff&password=", headers=form,
                   follow_redirects=False)
        assert "%C4%90%C3%A3%20l%C6%B0u" in r.headers["location"] and svc.ws.role_of("telegram:888") == "staff"
        evil = {**form, "Origin": "https://evil.example"}
        assert c.post("/admin/users/remove", content="id=telegram%3A888", headers=evil).status_code == 403
        c.post("/admin/grants/add", content="user=telegram%3A888&source=sales&hours=2", headers=form)
        assert svc.ws.sources_for("telegram:888", "staff") == ["sales"]
        assert "sales" in c.get("/admin").text
        preview = c.get("/admin/reports/bao_cao_sang/preview").text
        assert "Kết quả hôm qua" in preview
        assert "grant" in c.get("/admin/audit").text
        r = c.post("/admin/sync/muc_tieu_thang", headers=form, follow_redirects=False)
        assert "27" in r.headers["location"]

        login(c, "lan")                                                  # manager: chỉ cấp quyền
        assert c.get("/admin").status_code == 200
        assert c.post("/admin/users/add", content="id=telegram%3A9&role=staff", headers=form).status_code == 403
        r = c.post("/admin/grants/add", content="user=telegram%3A888&source=ke_hoach", headers=form,
                   follow_redirects=False)
        assert "L%E1%BB%97i" in r.headers["location"]                    # "Lỗi": manager không có ke_hoach
        assert c.get("/admin/audit").status_code == 403

        login(c, "nv")                                                   # staff: không vào được
        assert c.get("/admin").status_code == 403
