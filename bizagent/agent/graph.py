"""Graph LangGraph của agent: model <-> tool cho tới khi có câu trả lời.

Giai đoạn 1 dùng một agent duy nhất (gọn, dễ sửa). Các agent chuyên trách (analyst, coder, designer,
reviewer) của bản Nyna sẽ được thêm thành node riêng ở giai đoạn sau mà không đổi giao diện AgentService.
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
