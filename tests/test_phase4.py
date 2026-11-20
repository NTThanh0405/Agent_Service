"""Giai đoạn 4: lưu phiên chat vào DB, audit, MCP (client + server), kênh Zalo."""

import hashlib
import json
import sys
import textwrap

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from bizagent.agent.service import AgentService
from bizagent.channels.common import handle_command
from bizagent.channels.zalo import ZaloChannel, ZaloClient, ZaloConfig, expected_mac
from bizagent.mcp_server import BizagentTools
from bizagent.state import StateStore
from bizagent.workspace import SourceConfig, UserEntry


# ----------------------------------------------------------------------------- lưu phiên
async def test_conversation_survives_restart(demo_ws, fake_model_cls):
    m1 = fake_model_cls(responses=[AIMessage(content="Chào anh A")], seen=[])
    s1 = AgentService(demo_ws, model=m1)
    await s1.ask("Tôi là A", user_id="t:1", role="admin", thread_id="x")
    await s1.aclose()

    m2 = fake_model_cls(responses=[AIMessage(content="Anh là A")], seen=[])
    s2 = AgentService(demo_ws, model=m2)                       # như khởi động lại server
    await s2.ask("Tôi là ai?", user_id="t:1", role="admin", thread_id="x")
    humans = [m.content for m in m2.seen[0] if isinstance(m, HumanMessage)]
    assert humans == ["Tôi là A", "Tôi là ai?"]                # nhớ được câu trước khi khởi động lại

    rows = s2.store.list_threads("t:1")
    assert rows[0]["thread_id"] == "x" and rows[0]["title"] == "Tôi là A"
    kinds = [r["kind"] for r in s2.store.audit_rows()]
    assert kinds.count("ask") == 2

    await s2.delete_thread("t:1", "x")
    assert s2.store.list_threads("t:1") == []
    graph = await s2.ensure_ready()
    assert await graph.checkpointer.aget({"configurable": {"thread_id": "t:1:x"}}) is None
    await s2.aclose()


def test_store_prune_and_kv(tmp_path):
    st = StateStore(f"sqlite:///{tmp_path.as_posix()}/s.db")
    for i in range(5):
        st.touch("u", f"t{i}", f"q{i}")
    assert {t for _, t in st.threads_to_prune(idle_days=30, max_per_user=3)} == {"t0", "t1"}
    assert st.threads_to_prune(idle_days=-1, max_per_user=99).__len__() == 5     # mọi cuộc đều "quá hạn"
    st.kv_set("k", {"a": 1})
    st.kv_set("k", {"a": 2})
    assert st.kv_get("k") == {"a": 2} and st.kv_get("khong") is None
    st.audit("u", "sql", sql="SELECT 1")
    assert st.audit_rows()[0]["detail"] == {"sql": "SELECT 1"}
    assert st.prune_audit(days=-1) == 1
    st.close()


async def test_chat_commands(demo_ws, fake_model_cls):
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[AIMessage(content="ok")] * 3, seen=[]))
    u = "telegram:1"
    assert "Chưa có" in await handle_command(svc, u, "admin", "/sessions")
    await handle_command(svc, u, "admin", "/new")
    first = svc.store.current_thread(u)
    await svc.ask("câu một", user_id=u, role="admin", thread_id=first)
    await handle_command(svc, u, "admin", "/new")
    second = svc.store.current_thread(u)
    assert second != first
    listing = await handle_command(svc, u, "admin", "/sessions")
    assert "câu một" in listing and "đang dùng" in listing
    await handle_command(svc, u, "admin", "/chon 2")
    assert svc.store.current_thread(u) == first
    assert "Cách dùng" in await handle_command(svc, u, "admin", "/chon 9")
    assert "/login/link?t=" in await handle_command(svc, u, "admin", "/login")
    assert await handle_command(svc, u, "admin", "doanh thu?") is None
    await svc.aclose()


# ----------------------------------------------------------------------------- MCP client
FAKE_MCP = textwrap.dedent('''
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("fake-crm")

    @mcp.tool()
    def search_contacts(name: str) -> str:
        """Tìm khách hàng theo tên"""
        return f"Khách: {name} - 0901234567"

    @mcp.tool()
    def delete_everything() -> str:
        """Xoá toàn bộ dữ liệu"""
        return "ĐÃ XOÁ"

    mcp.run()
''')


async def test_mcp_source_tools(demo_ws, fake_model_cls, tmp_path):
    pytest.importorskip("mcp")                        # cần pip install "bizagent[mcp]"
    pytest.importorskip("langchain_mcp_adapters")
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_MCP, encoding="utf-8")
    demo_ws.sources["crm"] = SourceConfig(type="mcp", description="CRM", transport="stdio",
                                          command=sys.executable, args=[str(script)],
                                          allowed_tools=["search_contacts"], max_output_chars=12)
    demo_ws.info.roles["manager"] = ["sales", "warehouse"]          # manager không có crm
    model = fake_model_cls(responses=[
        AIMessage(content="", tool_calls=[{"name": "crm__search_contacts", "args": {"name": "Lan"}, "id": "1"}]),
        AIMessage(content="xong"),
        AIMessage(content="", tool_calls=[{"name": "crm__search_contacts", "args": {"name": "Lan"}, "id": "2"}]),
        AIMessage(content="xong"),
    ], seen=[])
    svc = AgentService(demo_ws, model=model)
    await svc.ensure_ready()
    assert svc.mcp_errors == {}
    assert "crm__search_contacts" in svc.tool_names and "crm__delete_everything" not in svc.tool_names

    await svc.ask("tìm Lan", user_id="t:1", role="admin", thread_id="a")
    tool_out = model.seen[1][-1].content
    assert tool_out.startswith("Khách: Lan -") and "đã cắt" in tool_out          # max_output_chars

    await svc.ask("tìm Lan", user_id="t:2", role="manager", thread_id="a")
    assert "không có quyền" in model.seen[3][-1].content
    assert any(r["kind"] == "mcp" for r in svc.store.audit_rows())
    await svc.aclose()


async def test_mcp_bad_config_is_isolated(demo_ws, fake_model_cls):
    demo_ws.sources["crm"] = SourceConfig(type="mcp", transport="stdio", command="khong-co-lenh-nay-xyz",
                                          allowed_tools=["a"], timeout_seconds=10)
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    await svc.ensure_ready()
    assert "crm" in svc.mcp_errors and "run_sql" in svc.tool_names
    await svc.aclose()


def test_mcp_source_requires_allowlist(demo_ws):
    from bizagent.sources.registry import SourceRegistry

    demo_ws.sources["crm"] = SourceConfig(type="mcp", transport="stdio", command="x")
    reg = SourceRegistry(demo_ws)
    assert "allowed_tools" in reg.errors["crm"]
    reg.close()


# ----------------------------------------------------------------------------- MCP server
def test_mcp_server_tools(demo_ws):
    from bizagent.sources.base import SourceError

    t = BizagentTools(demo_ws, "manager")
    assert {s["name"] for s in json.loads(t.list_sources())} == {"sales", "warehouse"}
    assert t.run_sql("sales", "SELECT COUNT(*) AS n FROM orders").startswith("n\n")
    with pytest.raises(SourceError):
        t.run_sql("ke_hoach", "SELECT 1")                          # manager không có nguồn này
    with pytest.raises(Exception):  # noqa: B017
        t.run_sql("sales", "DELETE FROM orders")
    ds = json.loads(t.get_dataset("top_mon", period="tháng này", max_rows=3))
    assert ds["columns"][0] == "mon" and len(ds["rows"]) <= 3
    assert "Doanh thu" in t.business_context()
    assert json.loads(t.get_date_range("hôm qua"))["start"]
    assert any(r["kind"] == "sql_denied" for r in t.store.audit_rows())
    t.close()


async def test_mcp_server_registers_tools(demo_ws):
    pytest.importorskip("mcp")                        # cần pip install "bizagent[mcp]"
    from bizagent.mcp_server import build_server

    mcp, t = build_server(demo_ws, "manager")
    names = {tool.name for tool in await mcp.list_tools()}
    assert {"run_sql", "list_sources", "get_dataset", "business_context"} <= names
    t.close()


# ----------------------------------------------------------------------------- Zalo
@pytest.fixture
def zalo_env(monkeypatch):
    for k, v in {"ZALO_APP_ID": "app1", "ZALO_APP_SECRET": "s", "ZALO_OA_SECRET_KEY": "oasec",
                 "ZALO_REFRESH_TOKEN": "r0", "ZALO_WEBHOOK_SECRET": "hook123"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ZALO_ACCESS_TOKEN", raising=False)


class FakeZalo:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.refreshes = 0
        self.fail_once = True

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if "oauth" in str(req.url):
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": f"a{self.refreshes}",
                                             "refresh_token": f"r{self.refreshes}", "expires_in": "3600"})
        body = json.loads(req.content)
        if self.fail_once:
            self.fail_once = False
            return httpx.Response(200, json={"error": -216, "message": "Access token is invalid"})
        self.sent.append((body["recipient"]["user_id"], body["message"]["text"]))
        return httpx.Response(200, json={"error": 0, "message": "Success"})


async def test_zalo_client_token_refresh(demo_ws, zalo_env):
    st = StateStore.for_workspace(demo_ws)
    fake = FakeZalo()
    client = ZaloClient(st, transport=httpx.MockTransport(fake))
    await client.send_text("u1", "xin chào")
    assert fake.sent == [("u1", "xin chào")] and fake.refreshes == 2          # lỗi token -> làm mới -> gửi lại
    assert st.kv_get("zalo.token")["refresh_token"] == "r2"                   # refresh token mới được lưu
    await client.send_text("u1", "x" * 4500)                                  # cắt thành 3 tin
    assert len(fake.sent) == 4
    await client.aclose()
    st.close()


def test_zalo_webhook(demo_ws, fake_model_cls, zalo_env, monkeypatch):
    from fastapi.testclient import TestClient

    from bizagent.channels.rest import create_app

    monkeypatch.setenv("BIZAGENT_SECRET_KEY", "k")
    (demo_ws.path / "channels.yaml").write_text("zalo: {enabled: true}\n", encoding="utf-8")
    demo_ws.users.append(UserEntry(id="zalo:777", role="manager"))
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[AIMessage(content="Doanh thu tốt")], seen=[]))
    fake = FakeZalo()
    fake.fail_once = False
    handled = []

    def event(sender: str, text: str, msg_id: str) -> tuple[bytes, dict]:
        body = json.dumps({"app_id": "app1", "event_name": "user_send_text", "timestamp": "1700000000",
                           "sender": {"id": sender}, "message": {"text": text, "msg_id": msg_id}}).encode()
        mac = expected_mac("app1", body, "1700000000", "oasec")
        return body, {"X-ZEvent-Signature": f"mac={mac}", "Content-Type": "application/json"}

    app = create_app(svc)
    with TestClient(app) as c:
        channel = app.state.zalo                       # thay client gửi tin bằng bản giả
        channel.client = ZaloClient(svc.store, transport=httpx.MockTransport(fake))
        orig = channel.handle

        async def handle_and_record(payload):
            await orig(payload)
            handled.append(payload["message"]["msg_id"])
        channel.handle = handle_and_record

        body, headers = event("777", "doanh thu hôm qua?", "m1")
        assert c.post("/zalo/webhook/sai", content=body, headers=headers).status_code == 404
        bad = {**headers, "X-ZEvent-Signature": "mac=" + hashlib.sha256(b"x").hexdigest()}
        assert c.post("/zalo/webhook/hook123", content=body, headers=bad).status_code == 401
        assert c.post("/zalo/webhook/hook123", content=body, headers=headers).status_code == 200
        c.post("/zalo/webhook/hook123", content=body, headers=headers)        # gửi lại cùng msg_id
        body2, h2 = event("999", "/myid", "m2")                                 # người lạ hỏi ID
        c.post("/zalo/webhook/hook123", content=body2, headers=h2)
        body3, h3 = event("999", "cho xem doanh thu", "m3")                     # người lạ hỏi dữ liệu
        c.post("/zalo/webhook/hook123", content=body3, headers=h3)
        for _ in range(100):
            if len(handled) >= 4:
                break
            c.get("/api/v1/health")
        assert ("777", "Doanh thu tốt") in fake.sent
        assert ("999", "Zalo ID của bạn: 999") in fake.sent
        assert sum(1 for u, _ in fake.sent if u == "777") == 1                  # không trả lời trùng
        assert not any(u == "999" and "doanh thu" in t.lower() for u, t in fake.sent)


def test_zalo_mac_formula():
    assert expected_mac("a", b'{"x":1}', "123", "s") == hashlib.sha256(b'a{"x":1}123s').hexdigest()
    ch = ZaloChannel.__new__(ZaloChannel)
    ch.config = ZaloConfig(enabled=True, signature="off")
    assert ch.verify(b"{}", {}, "")
