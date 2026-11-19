"""AgentService: điểm vào duy nhất cho mọi kênh (REST, CLI, Telegram, Zalo, ...).

    service = AgentService.from_workspace("demo")
    answer = await service.ask("doanh thu tuần này?", user_id="api:me", role="admin", thread_id="t1")
    ...
    await service.aclose()

Phần cần event loop (kết nối lưu phiên chat, nạp tool từ MCP server) được dựng ở lần ask() đầu tiên
hoặc khi gọi `await service.ensure_ready()`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from bizagent.agent.graph import build_graph
from bizagent.agent.tools import build_tools
from bizagent.dashboards.config import DashboardCatalog
from bizagent.dashboards.engine import DashboardEngine
from bizagent.llm.registry import ResolvedModel, build_chat_model, resolve_model
from bizagent.prompts.engine import PromptEngine
from bizagent.sources.registry import SourceRegistry
from bizagent.state import StateStore, open_checkpointer
from bizagent.workspace import Workspace, WorkspaceError, load_workspace

log = logging.getLogger(__name__)


def message_text(msg: Any) -> str:
    """Nội dung chữ của tin nhắn (một số model trả content dạng danh sách khối)."""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


class AgentService:
    def __init__(self, ws: Workspace, model: Any = None, checkpointer: Any = None):
        self.ws = ws
        self.registry = SourceRegistry(ws)
        self.store = StateStore.for_workspace(ws)
        self.store.attach(ws)                     # người dùng + quyền cấp riêng trong DB
        self.prompts = PromptEngine(ws)
        self.model_ref: ResolvedModel = resolve_model(ws.agents)
        self.settings = ws.agents.agent
        self.catalog: DashboardCatalog | None = None
        self.catalog_error: str | None = None
        try:
            self.catalog = DashboardCatalog(ws)
        except WorkspaceError as e:
            self.catalog_error = str(e)
            log.error("Dashboard lỗi cấu hình: %s", e)
        self.dashboard_engine = DashboardEngine(self.registry, self.catalog) if self.catalog else None
        self._model = model if model is not None else build_chat_model(self.model_ref, self.settings)
        self._checkpointer_override = checkpointer
        self._graph = None
        self._ready_lock = asyncio.Lock()
        self._stack = AsyncExitStack()
        self.mcp_errors: dict[str, str] = {}

    @classmethod
    def from_workspace(cls, name_or_path: str | None = None, **kwargs: Any) -> AgentService:
        return cls(load_workspace(name_or_path), **kwargs)

    def audit(self, user_id: str, kind: str, **detail: Any) -> None:
        self.store.audit(user_id, kind, **detail)

    # ------------------------------------------------------------------ dựng graph (cần event loop)
    async def ensure_ready(self):
        if self._graph is not None:
            return self._graph
        async with self._ready_lock:
            if self._graph is not None:
                return self._graph
            checkpointer = self._checkpointer_override
            if checkpointer is None:
                checkpointer = await open_checkpointer(self.store, self._stack)
            from bizagent.sources.mcp import load_mcp_tools

            mcp_tools, self.mcp_errors = await load_mcp_tools(self.registry, self.audit)
            tools = build_tools(self.registry, self.catalog, audit=self.audit) + mcp_tools
            self._graph = build_graph(self._model, tools, self.system_prompt_for, self.model_ref.capabilities,
                                      checkpointer)
            self.tool_names = [t.name for t in tools]
            return self._graph

    # ------------------------------------------------------------------ prompt
    def render_system_prompt(self, role: str | None, user_id: str | None = None) -> str:
        sources = [
            {"name": s.name, "kind": s.kind, "dialect": s.dialect, "description": s.description}
            for s in self.registry.for_role(role, user_id)
        ]
        return self.prompts.render("system.md.j2", role=role or "(chưa xác định)", sources=sources)

    def system_prompt_for(self, config: RunnableConfig) -> str:
        conf = config.get("configurable") or {}
        return self.render_system_prompt(conf.get("role"), conf.get("user_id"))

    # ------------------------------------------------------------------ hỏi đáp
    async def ask(self, text: str, *, user_id: str, role: str | None, thread_id: str) -> str:
        if role is None:
            return "Bạn chưa được cấp quyền dùng trợ lý này. Hãy liên hệ quản trị viên."
        graph = await self.ensure_ready()
        config: RunnableConfig = {
            "configurable": {"thread_id": f"{user_id}:{thread_id}", "role": role, "user_id": user_id},
            "recursion_limit": self.settings.max_steps * 2 + 1,
        }
        started = time.monotonic()
        outcome = "ok"
        try:
            state = await asyncio.wait_for(
                graph.ainvoke({"messages": [HumanMessage(content=text)]}, config),
                timeout=self.settings.timeout_seconds,
            )
            answer = "(không có câu trả lời)"
            for msg in reversed(state["messages"]):
                if isinstance(msg, AIMessage) and not msg.tool_calls:
                    answer = message_text(msg).strip() or "(không có nội dung)"
                    break
            answer = self._review(answer, state["messages"], user_id)
        except TimeoutError:
            outcome, answer = "timeout", "Câu hỏi xử lý quá lâu. Hãy thử hỏi ngắn gọn hoặc thu hẹp khoảng thời gian."
        except GraphRecursionError:
            outcome, answer = "too_many_steps", "Câu hỏi quá phức tạp (vượt số bước cho phép). Hãy chia nhỏ câu hỏi."
        except Exception as e:
            outcome = "error"
            self.audit(user_id, "ask", thread=thread_id, outcome=outcome, error=str(e)[:500])
            raise
        await asyncio.to_thread(self.store.touch, user_id, thread_id, text[:80])
        self.audit(user_id, "ask", thread=thread_id, outcome=outcome, chars=len(text),
                   seconds=round(time.monotonic() - started, 2))
        return answer

    def _review(self, answer: str, messages: list, user_id: str) -> str:
        """Reviewer (agents.yaml: reviewer.enabled): mọi con số trong câu trả lời phải đối chiếu được với dữ liệu
        tool trả về trong lượt này. Không gọi AI, không tốn tiền."""
        cfg = self.ws.agents.reviewer
        if not cfg.enabled:
            return answer
        from langchain_core.messages import ToolMessage

        from bizagent.numcheck import unverified

        last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        data = [message_text(m) for m in messages[last_human + 1:] if isinstance(m, ToolMessage)]
        if not data:                      # không truy vấn dữ liệu trong lượt này: chỉ soát khi có số liệu
            data = [message_text(m) for m in messages if isinstance(m, ToolMessage)]
        bad = unverified(answer, data, cfg.min_value)
        if bad:
            self.audit(user_id, "review_flag", numbers=bad)
            if cfg.add_note:
                answer += ("\n\n⚠️ Lưu ý: chưa đối chiếu được với dữ liệu đã truy vấn các con số: "
                           + ", ".join(bad[:8]) + ". Hãy kiểm tra lại trước khi dùng.")
        return answer

    async def delete_thread(self, user_id: str, thread_id: str) -> None:
        graph = await self.ensure_ready()
        if graph.checkpointer is not None:
            await graph.checkpointer.adelete_thread(f"{user_id}:{thread_id}")
        await asyncio.to_thread(self.store.delete_thread, user_id, thread_id)

    async def prune(self) -> int:
        """Xoá cuộc trò chuyện cũ / vượt hạn mức và audit cũ. Trả về số cuộc đã xoá."""
        cfg = self.ws.info.state
        victims = await asyncio.to_thread(self.store.threads_to_prune, cfg.idle_days, cfg.max_threads_per_user)
        for uid, tid in victims:
            await self.delete_thread(uid, tid)
        await asyncio.to_thread(self.store.prune_audit, cfg.audit_days)
        return len(victims)

    # ------------------------------------------------------------------ tác vụ nền
    def start_background_tasks(self) -> list[asyncio.Task]:
        """Lịch đồng bộ (sync.yaml, nếu run_in_server) + xuất file Power BI (nếu có powerbi.export)
        + dọn phiên chat cũ mỗi giờ. Gọi trong event loop của server/bot."""
        from bizagent.dashboards.powerbi import PowerBIExporter
        from bizagent.sync import build_runner

        if os.environ.get("BIZAGENT_BACKGROUND_TASKS", "1") == "0":
            # nhiều tiến trình cùng workspace: chỉ MỘT tiến trình chạy tác vụ nền (tránh gửi báo cáo trùng)
            log.info("BIZAGENT_BACKGROUND_TASKS=0: tiến trình này không chạy đồng bộ, báo cáo, xuất Power BI")
            return []
        tasks: list[asyncio.Task] = [asyncio.create_task(self._prune_forever())]
        runner = build_runner(self.ws, self.registry)
        self.sync_runner = runner
        if runner is not None and runner.config.run_in_server:
            tasks.append(asyncio.create_task(runner.run_forever()))
        from bizagent.reports import ReportRunner

        try:
            self.report_runner = ReportRunner(self)
            if self.report_runner.config.reports:
                tasks.append(asyncio.create_task(self.report_runner.run_forever()))
        except WorkspaceError as e:
            log.error("Báo cáo định kỳ lỗi cấu hình: %s", e)
        pbi = self.catalog.config.powerbi if self.catalog else None
        if pbi and pbi.enabled and pbi.export:
            self.powerbi_exporter = PowerBIExporter(self.dashboard_engine)
            tasks.append(asyncio.create_task(self.powerbi_exporter.run_forever()))
        return tasks

    async def _prune_forever(self) -> None:
        while True:
            try:
                n = await self.prune()
                if n:
                    log.info("Đã xoá %d cuộc trò chuyện cũ", n)
            except Exception:
                log.exception("Dọn phiên chat lỗi")
            await asyncio.sleep(3600)

    def sync_status(self) -> dict[str, Any]:
        runner = getattr(self, "sync_runner", None)
        if runner is None:
            return {}
        try:
            return {
                job: {"status": s["status"], "last_success": str(s["last_success"] or ""), "rows": s["rows"],
                      **({"error": s["error"]} if s["status"] != "ok" else {})}
                for job, s in runner.warehouse.sync_state().items()
            }
        except Exception as e:
            return {"error": str(e)}

    async def aclose(self) -> None:
        await self._stack.aclose()
        self.close()

    def close(self) -> None:
        self.registry.close()
        self.store.close()
