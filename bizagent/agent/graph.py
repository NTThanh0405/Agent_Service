"""Graph LangGraph của chế độ một agent (mặc định): model <-> tool cho tới khi có câu trả lời.

Chế độ nhiều agent chuyên trách (router, analyst, advisor, designer) nằm ở `bizagent.agent.team`, bật bằng
agents.yaml: multi_agent.enabled. Hai chế độ dùng chung giao diện AgentService.ask().
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from bizagent.llm.registry import ModelCapabilities


def message_text(msg: Any) -> str:
    """Nội dung chữ của tin nhắn (một số model trả content dạng danh sách khối)."""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def system_message(text: str, caps: ModelCapabilities) -> SystemMessage:
    """System prompt; đánh dấu cache khi model hỗ trợ (giảm chi phí cho prompt dài lặp lại)."""
    if caps.prompt_caching:
        return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])
    return SystemMessage(content=text)


def build_graph(
    model: Any,
    tools: list[BaseTool],
    system_prompt_for: Callable[[RunnableConfig], str],
    caps: ModelCapabilities,
    checkpointer: Any = None,
):
    if not caps.tool_calling:
        raise ValueError("Model này không hỗ trợ gọi tool nên không dùng được cho agent truy vấn dữ liệu")
    bound = model.bind_tools(tools)

    async def agent(state: MessagesState, config: RunnableConfig) -> dict:
        prompt = system_message(system_prompt_for(config), caps)
        response = await bound.ainvoke([prompt, *state["messages"]], config)
        return {"messages": [response]}

    g = StateGraph(MessagesState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
