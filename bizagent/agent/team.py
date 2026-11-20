"""Chế độ nhiều agent chuyên trách (agents.yaml: multi_agent.enabled: true).

    START -> router --phan_tich / tra_cuu--> analyst <-> analyst_tools --phan_tich + có số liệu--> advisor -> END
                    \\--dashboard---------> designer <-> designer_tools -> END

- router: model rẻ, không gọi tool, chỉ chọn đường (phan_tich | tra_cuu | dashboard). Tắt: mọi câu đi analyst.
- analyst: truy vấn dữ liệu bằng tool như agent đơn (cùng quyền theo role, cùng guard SQL).
- advisor: không gọi tool; CHỈ đọc kết quả tool analyst vừa lấy + câu trả lời của analyst, viết phần
  "Đánh giá & đề xuất" nối vào cuối câu trả lời. Không có số liệu trong lượt này thì bỏ qua.
- designer: tìm / tạo dashboard (create_dashboard). Tắt: analyst được dùng cả tool dashboard.

Router và advisor nhận lịch sử dạng chữ (không có tool_call) nên dùng được với model không hỗ trợ tool.
Reviewer đối chiếu số (AgentService._review) vẫn chạy trên câu trả lời cuối, gồm cả phần đề xuất.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from bizagent.agent.graph import message_text, system_message
from bizagent.llm.registry import ModelCapabilities
from bizagent.workspace import MultiAgentSettings

log = logging.getLogger(__name__)

ROUTES = ("phan_tich", "tra_cuu", "dashboard")
DESIGNER_ONLY_TOOLS = {"create_dashboard"}
# Kết quả tool không phải số liệu kinh doanh (schema, danh sách nguồn): không đưa cho advisor
NON_DATA_TOOLS = {"list_sources", "list_tables", "describe_table", "list_dashboards"}
MAX_TOOL_CHARS = 6000          # mỗi kết quả tool đưa cho advisor
MAX_DATA_CHARS = 24000         # tổng dữ liệu đưa cho advisor
# Prompt của từng agent: (tên agent, config, biến thêm) -> nội dung system prompt
PromptFn = Callable[..., str]


@dataclass
class Member:
    model: Any
    caps: ModelCapabilities
    max_steps: int = 12


class TeamState(MessagesState):
    route: str
    steps: dict[str, int]


_text = message_text


def current_turn(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Tin nhắn từ câu hỏi mới nhất trở đi."""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    return messages[last_human:] if last_human >= 0 else list(messages)


def data_results(messages: list[BaseMessage]) -> list[tuple[str, str, str]]:
    """(tool, tham số, kết quả) của các lần gọi tool ra số liệu trong lượt này (bỏ lỗi và tool xem schema)."""
    turn = current_turn(messages)
    args = {c["id"]: c.get("args") for m in turn if isinstance(m, AIMessage) for c in m.tool_calls}
    out = []
    for m in turn:
        if not isinstance(m, ToolMessage) or m.name in NON_DATA_TOOLS or m.status == "error":
            continue
        text = _text(m).strip()
        if not text or text.startswith("Lỗi"):
            continue
        out.append((m.name or "tool", json.dumps(args.get(m.tool_call_id) or {}, ensure_ascii=False), text))
    return out


def history_text(messages: list[BaseMessage], turns: int = 4, max_chars: int = 600) -> str:
    """Vài lượt hỏi/đáp gần nhất trước câu hỏi hiện tại, dạng chữ (bỏ tool_call và kết quả tool)."""
    turn_start = len(messages) - len(current_turn(messages))
    lines = []
    for m in messages[:turn_start]:
        if isinstance(m, HumanMessage):
            lines.append(("Người dùng", _text(m)))
        elif isinstance(m, AIMessage) and not m.tool_calls and _text(m).strip():
            lines.append(("Trợ lý", _text(m)))
    return "\n".join(f"{who}: {t.strip()[:max_chars]}" for who, t in lines[-turns * 2:])


def parse_route(text: str, allowed: tuple[str, ...]) -> str:
    """Đường router chọn; không nhận ra thì mặc định phan_tich. Chấp nhận cả "Phân tích", "tra-cuu"..."""
    t = unicodedata.normalize("NFD", text.strip().lower().replace("đ", "d"))
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("-", "_").replace(" ", "_")
    for r in ("dashboard", "tra_cuu", "phan_tich"):
        if r in t and r in allowed:
            return r
    return "phan_tich"


def build_team_graph(
    members: dict[str, Member],
    tools: list[BaseTool],
    prompt_for: PromptFn,
    cfg: MultiAgentSettings,
    checkpointer: Any = None,
):
    """members: "router", "analyst", "advisor", "designer" (chỉ các agent đang bật; analyst bắt buộc)."""
    analyst = members["analyst"]
    if not analyst.caps.tool_calling:
        raise ValueError("Model của analyst không hỗ trợ gọi tool nên không truy vấn được dữ liệu")
    use_router = cfg.router.enabled and "router" in members
    use_designer = use_router and cfg.designer.enabled and "designer" in members and any(
        t.name in DESIGNER_ONLY_TOOLS for t in tools)
    use_advisor = cfg.advisor.enabled and "advisor" in members
    if use_designer and not members["designer"].caps.tool_calling:
        raise ValueError("Model của designer không hỗ trợ gọi tool")
    analyst_tools = [t for t in tools if t.name not in DESIGNER_ONLY_TOOLS] if use_designer else list(tools)
    allowed = ("phan_tich", "tra_cuu", "dashboard") if use_designer else ("phan_tich", "tra_cuu")

    # ------------------------------------------------------------------ router
    async def router(state: TeamState, config: RunnableConfig) -> dict:
        route = "phan_tich"
        if use_router:
            m = members["router"]
            question = _text(current_turn(state["messages"])[0])
            history = history_text(state["messages"])
            user = (f"Hội thoại trước:\n{history}\n\n" if history else "") + f"Câu hỏi mới:\n{question}"
            prompt = system_message(prompt_for("router", config, routes=allowed), m.caps)
            try:
                reply = await m.model.ainvoke([prompt, HumanMessage(content=user)], config)
                route = parse_route(_text(reply), allowed)
            except Exception as e:                  # router lỗi: vẫn trả lời theo đường mặc định
                log.warning("Router lỗi, dùng đường phan_tich: %s", e)
        return {"route": route, "steps": {}}          # mỗi câu hỏi đếm lại số vòng gọi tool

    # ------------------------------------------------------------------ agent gọi tool (analyst, designer)
    def tool_agent(name: str, member: Member, agent_tools: list[BaseTool]):
        bound = member.model.bind_tools(agent_tools)

        async def run(state: TeamState, config: RunnableConfig) -> dict:
            route = state.get("route") or "phan_tich"
            extra = {"route": route, "advisor_follows": use_advisor and route == "phan_tich"}
            prompt = system_message(prompt_for(name, config, **extra), member.caps)
            response = await bound.ainvoke([prompt, *state["messages"]], config)
            steps = dict(state.get("steps") or {})
            if getattr(response, "tool_calls", None):
                if steps.get(name, 0) >= member.max_steps:
                    raise GraphRecursionError(f"{name} vượt max_steps={member.max_steps}")
                steps[name] = steps.get(name, 0) + 1
            return {"messages": [response], "steps": steps}

        return run

    def after_tool_agent(name: str, follow: str | None):
        def decide(state: TeamState) -> str:
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls:
                return f"{name}_tools"
            if follow and state.get("route") == "phan_tich" and _text(last).strip() and data_results(
                    state["messages"]):
                return follow
            return END

        return decide

    # ------------------------------------------------------------------ advisor
    async def advisor(state: TeamState, config: RunnableConfig) -> dict:
        m = members["advisor"]
        messages = state["messages"]
        final = messages[-1]
        draft = _text(final).strip()
        parts, used = [], 0
        for tool_name, args, text in data_results(messages):
            chunk = text[:MAX_TOOL_CHARS] + ("\n...(đã cắt bớt)" if len(text) > MAX_TOOL_CHARS else "")
            if used + len(chunk) > MAX_DATA_CHARS:
                parts.append("...(còn kết quả khác, đã lược bớt)")
                break
            used += len(chunk)
            parts.append(f"### {tool_name} {args}\n{chunk}")
        user = (f"## Câu hỏi\n{_text(current_turn(messages)[0])}\n\n"
                f"## Dữ liệu agent phân tích đã truy vấn\n" + "\n\n".join(parts) + "\n\n"
                f"## Câu trả lời của agent phân tích\n{draft}")
        try:
            prompt = system_message(prompt_for("advisor", config), m.caps)
            reply = await m.model.ainvoke([prompt, HumanMessage(content=user)], config)
            advice = _text(reply).strip()
        except Exception as e:                      # advisor lỗi: vẫn trả phần số liệu của analyst
            log.warning("Advisor lỗi, trả câu trả lời của analyst: %s", e)
            return {}
        if not advice:
            return {}
        # Thay tin nhắn cuối của analyst (cùng id) bằng câu trả lời đầy đủ: lịch sử khớp với cái người dùng thấy
        return {"messages": [final.model_copy(update={"content": f"{draft}\n\n{advice}"})]}

    # ------------------------------------------------------------------ ghép graph
    g = StateGraph(TeamState)
    g.add_node("router", router)
    g.add_node("analyst", tool_agent("analyst", analyst, analyst_tools))
    g.add_node("analyst_tools", ToolNode(analyst_tools))
    g.add_edge(START, "router")
    g.add_edge("analyst_tools", "analyst")
    targets = {"analyst_tools": "analyst_tools", END: END}
    if use_advisor:
        g.add_node("advisor", advisor)
        g.add_edge("advisor", END)
        targets["advisor"] = "advisor"
    g.add_conditional_edges("analyst", after_tool_agent("analyst", "advisor" if use_advisor else None), targets)
    if use_designer:
        g.add_node("designer", tool_agent("designer", members["designer"], list(tools)))
        g.add_node("designer_tools", ToolNode(list(tools)))
        g.add_edge("designer_tools", "designer")
        g.add_conditional_edges("designer", after_tool_agent("designer", None),
                                {"designer_tools": "designer_tools", END: END})
        g.add_conditional_edges("router", lambda s: "designer" if s.get("route") == "dashboard" else "analyst",
                                ["analyst", "designer"])
    else:
        g.add_edge("router", "analyst")
    return g.compile(checkpointer=checkpointer)


def recursion_limit(members: dict[str, Member]) -> int:
    """Giới hạn số bước của cả graph: đủ rộng cho max_steps từng agent (giới hạn thật nằm trong từng agent)."""
    return sum(m.max_steps for n, m in members.items() if n in ("analyst", "designer")) * 2 + 10
