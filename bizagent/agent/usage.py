"""Đếm token của MỌI lượt gọi model trong một câu hỏi, theo từng agent.

Gắn vào config["callbacks"] khi chạy graph: mỗi lần chat model chạy xong, đọc `usage_metadata` của tin nhắn model
trả về và cộng vào agent đang chạy (tên node LangGraph: "agent" ở chế độ một agent; "router", "analyst",
"advisor", "designer" ở chế độ nhiều agent). Lượt gọi xong trước khi câu hỏi bị timeout vẫn được tính.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler


class TokenMeter(BaseCallbackHandler):
    run_inline = True                  # chạy ngay trong event loop, không qua thread pool

    def __init__(self) -> None:
        self.by_agent: dict[str, dict[str, int]] = {}
        self._runs: dict[UUID, str] = {}

    def _agent(self, metadata: dict[str, Any] | None) -> str:
        return str((metadata or {}).get("langgraph_node") or "khac")

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, metadata=None, **kwargs: Any) -> None:
        self._runs[run_id] = self._agent(metadata)

    def on_llm_start(self, serialized, prompts, *, run_id: UUID, metadata=None, **kwargs: Any) -> None:
        self._runs[run_id] = self._agent(metadata)

    def on_llm_end(self, response, *, run_id: UUID, **kwargs: Any) -> None:
        agent = self._runs.pop(run_id, "khac")
        row = self.by_agent.setdefault(agent, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
        row["calls"] += 1
        for gens in response.generations:
            for gen in gens:
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
                if usage:
                    row["tokens_in"] += int(usage.get("input_tokens") or 0)
                    row["tokens_out"] += int(usage.get("output_tokens") or 0)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._runs.pop(run_id, None)

    def totals(self) -> tuple[int, int]:
        return (sum(r["tokens_in"] for r in self.by_agent.values()),
                sum(r["tokens_out"] for r in self.by_agent.values()))
