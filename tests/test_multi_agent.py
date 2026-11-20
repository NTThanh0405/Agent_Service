"""Chế độ nhiều agent (router -> analyst -> advisor, designer) và đếm token, với model giả."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from bizagent.agent.service import AgentService
from bizagent.agent.team import parse_route
from bizagent.workspace import AgentsConfig


def usage(i, o):
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}


def ai(content="", calls=None, u=(0, 0)):
    return AIMessage(content=content, tool_calls=calls or [], usage_metadata=usage(*u))


def call(name, args, id_):
    return [{"name": name, "args": args, "id": id_}]


ADVICE = "**Đánh giá & đề xuất**\n- Đánh giá: ổn định.\n- Đề xuất: giữ nhịp khuyến mãi."


def multi(ws, **members):
    ws.agents.multi_agent.enabled = True
    for name, values in members.items():
        for k, v in values.items():
            setattr(getattr(ws.agents.multi_agent, name), k, v)
    return ws


def last_audit(svc, kind="ask"):
    return next(r for r in svc.store.audit_rows(20) if r["kind"] == kind)["detail"]


def system_text(msgs):
    return str(msgs[0].content) if isinstance(msgs[0], SystemMessage) else ""


async def test_single_agent_counts_all_calls(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai(calls=call("get_date_range", {"expression": "hôm qua"}, "c0"), u=(100, 10)),
        ai("Xong.", u=(150, 20)),
    ], seen=[])
    svc = AgentService(demo_ws, model=model)
    assert not svc.multi_agent
    assert await svc.ask("hôm qua?", user_id="t:1", role="admin", thread_id="a") == "Xong."
    detail = last_audit(svc)
    assert (detail["tokens_in"], detail["tokens_out"]) == (250, 30)
    assert "agents" not in detail and "route" not in detail          # chế độ cũ: audit như trước
    assert svc.store.kv_get(f"usage:{svc._today()}:t:1")["tokens_in"] == 250
    await svc.aclose()


async def test_multi_agent_analysis_with_advice(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai("phan_tich", u=(30, 1)),                                                            # router
        ai(calls=call("get_date_range", {"expression": "hôm qua"}, "c0"), u=(200, 10)),        # analyst
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1"),
           u=(250, 15)),
        ai("Hôm qua có đơn hàng ổn định.", u=(300, 20)),
        ai(ADVICE, u=(400, 40)),                                                               # advisor
    ], seen=[])
    svc = AgentService(multi(demo_ws), model=model)
    answer = await svc.ask("đơn hàng hôm qua thế nào?", user_id="t:1", role="admin", thread_id="a")
    assert answer == "Hôm qua có đơn hàng ổn định.\n\n" + ADVICE
    assert not model.responses                                       # đủ 5 lượt gọi model

    router_in, analyst_in, advisor_in = model.seen[0], model.seen[1], model.seen[4]
    assert "bộ điều phối" in system_text(router_in) and "dashboard" in system_text(router_in)
    assert not any(isinstance(m, ToolMessage) for m in router_in + advisor_in)   # chỉ chữ, không tool_call
    assert "phân tích dữ liệu" in system_text(analyst_in) and "KHÔNG viết mục đề xuất" in system_text(analyst_in)
    assert "12,5 triệu" in system_text(analyst_in)                   # business_rules của workspace vẫn áp dụng
    assert "tư vấn kinh doanh" in system_text(advisor_in)
    data = advisor_in[-1].content
    assert "### run_sql" in data and "SELECT COUNT(*)" in data and '"start"' in data
    assert "describe_table" not in data
    assert "Hôm qua có đơn hàng ổn định." in data                    # thấy câu trả lời của analyst

    detail = last_audit(svc)
    assert (detail["tokens_in"], detail["tokens_out"]) == (1180, 86)  # cộng đủ mọi agent
    assert detail["route"] == "phan_tich"
    assert detail["agents"] == {
        "router": {"calls": 1, "tokens_in": 30, "tokens_out": 1},
        "analyst": {"calls": 3, "tokens_in": 750, "tokens_out": 45},
        "advisor": {"calls": 1, "tokens_in": 400, "tokens_out": 40},
    }

    # lượt sau: lịch sử giữ câu trả lời đầy đủ (có đề xuất), router nhận hội thoại trước dạng chữ
    model.responses = [ai("tra_cuu"), ai("Đề xuất là giữ nhịp khuyến mãi.")]
    answer = await svc.ask("đề xuất là gì?", user_id="t:1", role="admin", thread_id="a")
    assert answer == "Đề xuất là giữ nhịp khuyến mãi."
    assert "Trợ lý: Hôm qua có đơn" in model.seen[5][-1].content
    finals = [m for m in model.seen[6] if isinstance(m, AIMessage) and not m.tool_calls]
    assert finals[-1].content.endswith(ADVICE)
    await svc.aclose()


async def test_lookup_and_no_data_skip_advisor(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai("tra_cuu"),
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1")),
        ai("Đây là số đơn."),
        ai("phan_tich"),                                   # chào hỏi: không truy vấn dữ liệu -> không gọi advisor
        ai("Xin chào!"),
    ], seen=[])
    svc = AgentService(multi(demo_ws), model=model)
    assert await svc.ask("chỉ cần số đơn", user_id="t:1", role="admin", thread_id="a") == "Đây là số đơn."
    assert await svc.ask("chào", user_id="t:1", role="admin", thread_id="b") == "Xin chào!"
    assert not model.responses
    assert "KHÔNG viết mục đề xuất" not in system_text(model.seen[1])     # tra_cuu: không có advisor phía sau
    assert set(last_audit(svc)["agents"]) == {"router", "analyst"}
    await svc.aclose()


async def test_denied_data_skips_advisor(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai("phan_tich"),
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT 1 FROM orders"}, "c1")),
        ai("Bạn không có quyền xem dữ liệu bán hàng."),
    ], seen=[])
    svc = AgentService(multi(demo_ws), model=model)
    answer = await svc.ask("doanh thu?", user_id="t:2", role="staff", thread_id="a")
    assert answer == "Bạn không có quyền xem dữ liệu bán hàng."                  # quyền theo role giữ nguyên
    tool_msg = [m for m in model.seen[-1] if isinstance(m, ToolMessage)][0]
    assert "Không có quyền" in tool_msg.content
    await svc.aclose()


async def test_dashboard_route_uses_designer(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai("dashboard"),
        ai(calls=call("list_dashboards", {}, "c1")),
        ai("Dashboard tổng quan: /dashboards/tong_quan"),
    ], seen=[])
    svc = AgentService(multi(demo_ws), model=model)
    answer = await svc.ask("cho xem dashboard", user_id="t:1", role="admin", thread_id="a")
    assert answer.startswith("Dashboard tổng quan")
    assert "thiết kế dashboard" in system_text(model.seen[1])
    assert '"slug"' in [m for m in model.seen[2] if isinstance(m, ToolMessage)][0].content
    assert last_audit(svc)["route"] == "dashboard" and "designer" in last_audit(svc)["agents"]
    # analyst không được tạo dashboard khi có designer
    graph = await svc.ensure_ready()
    assert "create_dashboard" not in graph.nodes["analyst_tools"].bound.tools_by_name
    assert "create_dashboard" in graph.nodes["designer_tools"].bound.tools_by_name
    await svc.aclose()


async def test_router_and_designer_disabled(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1")),
        ai("Có đơn."),
        ai(ADVICE),
    ], seen=[])
    ws = multi(demo_ws, router={"enabled": False}, designer={"enabled": False})
    svc = AgentService(ws, model=model)
    assert (await svc.ask("đơn?", user_id="t:1", role="admin", thread_id="a")).endswith(ADVICE)
    graph = await svc.ensure_ready()
    assert "designer" not in graph.nodes and "create_dashboard" in graph.nodes["analyst_tools"].bound.tools_by_name
    assert set(last_audit(svc)["agents"]) == {"analyst", "advisor"}             # không gọi router
    await svc.aclose()


async def test_advisor_numbers_go_through_reviewer(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai("phan_tich"),
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1")),
        ai("Có đơn."),
        ai("**Đánh giá & đề xuất**\n- Đề xuất: đặt mục tiêu 987.654 đơn."),     # số bịa
    ], seen=[])
    svc = AgentService(multi(demo_ws), model=model)
    answer = await svc.ask("đơn?", user_id="t:1", role="admin", thread_id="a")
    assert "⚠️ Lưu ý" in answer and "987.654" in answer.split("⚠️")[1]
    await svc.aclose()


async def test_advisor_error_keeps_analyst_answer(demo_ws, fake_model_cls):
    model = fake_model_cls(responses=[
        ai("phan_tich"),
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1")),
        ai("Có đơn."),
    ], seen=[])                                        # hết câu trả lời: lượt gọi advisor ném lỗi
    svc = AgentService(multi(demo_ws), model=model)
    assert await svc.ask("đơn?", user_id="t:1", role="admin", thread_id="a") == "Có đơn."
    await svc.aclose()


async def test_per_agent_step_limit(demo_ws, fake_model_cls):
    loop = [ai("phan_tich")] + [ai(calls=call("list_sources", {}, f"c{i}")) for i in range(10)]
    model = fake_model_cls(responses=loop, seen=[])
    svc = AgentService(multi(demo_ws, analyst={"max_steps": 2}), model=model)
    assert "quá phức tạp" in await svc.ask("x", user_id="t:1", role="admin", thread_id="a")
    assert len(model.responses) == 11 - 4                         # router + 2 vòng tool + lượt thứ 3 bị chặn
    await svc.aclose()


async def test_workspace_overrides_agent_prompt(demo_ws, fake_model_cls):
    (demo_ws.prompts_dir / "advisor.md.j2").write_text(
        '{% extends "core/advisor.md.j2" %}{% block business_rules %}6. Ưu tiên chi nhánh {{ vars.tone }}.'
        "{% endblock %}", encoding="utf-8")
    model = fake_model_cls(responses=[
        ai("phan_tich"),
        ai(calls=call("run_sql", {"source": "sales", "sql": "SELECT COUNT(*) AS n FROM orders"}, "c1")),
        ai("Có đơn."),
        ai(ADVICE),
    ], seen=[])
    svc = AgentService(multi(demo_ws), model=model)
    await svc.ask("đơn?", user_id="t:1", role="admin", thread_id="a")
    text = system_text(model.seen[3])
    assert "6. Ưu tiên chi nhánh" in text and "Quy tắc bắt buộc" in text
    await svc.aclose()


def test_per_agent_models_and_settings(demo_ws, fake_model_cls):
    cfg = AgentsConfig(**{
        "default_model": "anthropic:claude-sonnet-5-5",
        "models": {"fast": "anthropic:claude-haiku-5-5", "strong": "anthropic:claude-opus-5-5"},
        "agent": {"max_steps": 12, "max_tokens": 3000},
        "multi_agent": {"enabled": True, "router": {"model": "fast", "max_tokens": 50},
                        "analyst": {"model": "strong", "max_steps": 20}},
    })
    assert cfg.settings_for("router").max_tokens == 50 and cfg.settings_for("router").max_steps == 12
    assert cfg.settings_for("analyst").max_steps == 20 and cfg.settings_for("advisor").model == "default"
    demo_ws.agents = cfg
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    labels = {k: v.label for k, v in svc.agent_model_refs.items()}
    assert labels == {"router": "anthropic:claude-haiku-5-5", "analyst": "anthropic:claude-opus-5-5",
                      "advisor": "anthropic:claude-sonnet-5-5", "designer": "anthropic:claude-sonnet-5-5"}
    assert svc._members["analyst"].max_steps == 20
    svc.close()


def test_config_validation():
    with pytest.raises(ValueError, match="analyst"):
        AgentsConfig(multi_agent={"analyst": {"enabled": False}})
    with pytest.raises(ValueError):
        AgentsConfig(multi_agent={"router": {"max_steps": 0}})
    with pytest.raises(ValueError):
        AgentsConfig(multi_agent={"coder": {}})
    assert not AgentsConfig().multi_agent.enabled                    # mặc định: một agent như cũ


def test_parse_route():
    allowed = ("phan_tich", "tra_cuu", "dashboard")
    assert parse_route("dashboard", allowed) == "dashboard"
    assert parse_route(" Tra cứu ", allowed) == "tra_cuu"
    assert parse_route("không rõ", allowed) == "phan_tich"           # không khớp -> mặc định phân tích
    assert parse_route("`tra_cuu`", allowed) == "tra_cuu"
    assert parse_route("dashboard", ("phan_tich", "tra_cuu")) == "phan_tich"
    assert parse_route("", allowed) == "phan_tich"


def test_history_text_skips_tool_messages():
    from bizagent.agent.team import history_text

    msgs = [HumanMessage(content="q1"), ai(calls=call("x", {}, "c")), ToolMessage(content="raw", tool_call_id="c"),
            ai("a1"), HumanMessage(content="q2")]
    assert history_text(msgs) == "Người dùng: q1\nTrợ lý: a1"
