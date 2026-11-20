"""Luồng agent đầy đủ với model giả: gọi tool thật trên SQLite, kiểm tra quyền theo role."""

from langchain_core.messages import AIMessage, ToolMessage

from bizagent import __version__
from bizagent.agent.service import AgentService


def tool_call(name, args, id_="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_}])


async def test_agent_runs_sql_and_answers(demo_ws, fake_model_cls):
    model = fake_model_cls(
        responses=[
            tool_call("get_date_range", {"expression": "hôm qua"}, "c0"),
            tool_call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1"),
            AIMessage(content="Có nhiều đơn."),
        ],
        seen=[],
    )
    svc = AgentService(demo_ws, model=model)
    answer = await svc.ask("có bao nhiêu đơn?", user_id="t:1", role="admin", thread_id="a")
    assert answer == "Có nhiều đơn."

    last_call = model.seen[-1]
    assert "Chuỗi cà phê Demo" in str(last_call[0].content)          # system prompt workspace
    tool_msgs = [m for m in last_call if isinstance(m, ToolMessage)]
    assert '"start"' in tool_msgs[0].content                           # get_date_range trả JSON
    assert tool_msgs[1].content.startswith("n\n")                      # run_sql trả bảng
    await svc.aclose()


async def test_role_without_source_is_denied(demo_ws, fake_model_cls):
    model = fake_model_cls(
        responses=[
            tool_call("run_sql", {"source": "sales", "sql": "SELECT 1 FROM orders"}),
            AIMessage(content="Không có quyền."),
        ],
        seen=[],
    )
    svc = AgentService(demo_ws, model=model)
    await svc.ask("đơn?", user_id="t:2", role="staff", thread_id="a")
    tool_msg = [m for m in model.seen[-1] if isinstance(m, ToolMessage)][0]
    assert "Không có quyền" in tool_msg.content
    assert "CHƯA được cấp nguồn" in str(model.seen[0][0].content)
    await svc.aclose()


async def test_unknown_user_gets_refusal(demo_ws, fake_model_cls):
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    assert "chưa được cấp quyền" in await svc.ask("x", user_id="t:3", role=None, thread_id="a")
    await svc.aclose()


async def test_step_limit(demo_ws, fake_model_cls):
    demo_ws.agents.agent.max_steps = 1
    loop = [tool_call("list_sources", {}, f"c{i}") for i in range(10)]
    svc = AgentService(demo_ws, model=fake_model_cls(responses=loop, seen=[]))
    assert "quá phức tạp" in await svc.ask("x", user_id="t:4", role="admin", thread_id="a")
    await svc.aclose()


def test_rest_api(demo_ws, fake_model_cls, monkeypatch):
    from fastapi.testclient import TestClient

    from bizagent.channels.rest import create_app

    monkeypatch.setenv("BIZAGENT_API_TOKEN", "s3cret")
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[AIMessage(content="Xin chào")], seen=[]))
    client = TestClient(create_app(svc))
    with client:                                  # chạy lifespan: tác vụ nền, mở/đóng kết nối lưu phiên
        assert client.get("/api/v1/health").json() == {"status": "ok", "version": __version__}   # không lộ chi tiết
        health = client.get("/api/v1/health", headers={"Authorization": "Bearer s3cret"}).json()
        assert health["sources"] == ["ke_hoach", "sales", "warehouse"]
        assert health["source_errors"] == {} and health["channels"]["zalo"] == {"enabled": False}
        assert client.post("/api/v1/chat", json={"message": "hi"}).status_code == 401
        r = client.post("/api/v1/chat", json={"message": "hi"}, headers={"Authorization": "Bearer s3cret"})
        assert r.json()["answer"] == "Xin chào"
        root = client.get("/", follow_redirects=False)                 # không có trang chat web
        assert root.status_code == 303 and root.headers["location"] == "/dashboards"
