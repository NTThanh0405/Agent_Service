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
from collections import deque
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from bizagent.agent.graph import build_graph, message_text
from bizagent.agent.team import Member, build_team_graph, recursion_limit
from bizagent.agent.tools import build_tools
from bizagent.agent.usage import TokenMeter
from bizagent.dashboards.config import DashboardCatalog
from bizagent.dashboards.engine import DashboardEngine
from bizagent.llm.registry import ResolvedModel, build_chat_model, resolve_model
from bizagent.prompts.engine import PromptEngine
from bizagent.sources.registry import SourceRegistry
from bizagent.state import StateStore, open_checkpointer
from bizagent.workspace import Workspace, WorkspaceError, load_workspace

log = logging.getLogger(__name__)


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
        # Chế độ nhiều agent: model + cấu hình riêng từng agent (model truyền vào khi test thì dùng cho mọi agent)
        self.multi_agent = ws.agents.multi_agent.enabled
        self.agent_model_refs: dict[str, ResolvedModel] = {}
        self._members: dict[str, Member] = {}
        if self.multi_agent:
            self._members = self._build_members(model)
        self._checkpointer_override = checkpointer
        self._graph = None
        self._ready_lock = asyncio.Lock()
        self._stack = AsyncExitStack()
        self.mcp_errors: dict[str, str] = {}
        self._slots = asyncio.Semaphore(ws.agents.limits.max_concurrent)   # số câu hỏi xử lý cùng lúc
        self._thread_locks: dict[str, asyncio.Lock] = {}                    # mỗi cuộc trò chuyện một lượt một lúc
        self._recent: dict[str, deque[float]] = {}                          # thời điểm câu hỏi gần đây theo người

    def _build_members(self, injected: Any) -> dict[str, Member]:
        cfg = self.ws.agents
        built: dict[tuple, Any] = {}
        members: dict[str, Member] = {}
        for name in ("router", "analyst", "advisor", "designer"):
            if not getattr(cfg.multi_agent, name).enabled:
                continue
            settings = cfg.settings_for(name)
            ref = resolve_model(cfg, settings.model)
            self.agent_model_refs[name] = ref
            if injected is not None:
                model = injected
            elif ref.label == self.model_ref.label and settings == self.settings:
                model = self._model
            else:                                   # agent cùng model + cùng cấu hình dùng chung một client
                key = (ref.label, ref.base_url, settings.max_tokens, settings.temperature, settings.timeout_seconds)
                if key not in built:
                    built[key] = build_chat_model(ref, settings)
                model = built[key]
            members[name] = Member(model=model, caps=ref.capabilities, max_steps=settings.max_steps)
        return members

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
            if self.multi_agent:
                self._graph = build_team_graph(self._members, tools, self.agent_prompt_for,
                                               self.ws.agents.multi_agent, checkpointer)
            else:
                self._graph = build_graph(self._model, tools, self.system_prompt_for, self.model_ref.capabilities,
                                          checkpointer)
            self.tool_names = [t.name for t in tools]
            return self._graph

    # ------------------------------------------------------------------ prompt
    def render_system_prompt(self, role: str | None, user_id: str | None = None,
                             template: str = "system.md.j2", **extra: Any) -> str:
        sources = [
            {"name": s.name, "kind": s.kind, "dialect": s.dialect, "description": s.description}
            for s in self.registry.for_role(role, user_id)
        ]
        return self.prompts.render(template, role=role or "(chưa xác định)", sources=sources, **extra)

    def system_prompt_for(self, config: RunnableConfig) -> str:
        conf = config.get("configurable") or {}
        return self.render_system_prompt(conf.get("role"), conf.get("user_id"))

    def agent_prompt_for(self, agent: str, config: RunnableConfig, **extra: Any) -> str:
        """Prompt của một agent chuyên trách: prompts/<agent>.md.j2 (ghi đè được theo workspace)."""
        conf = config.get("configurable") or {}
        return self.render_system_prompt(conf.get("role"), conf.get("user_id"), f"{agent}.md.j2", **extra)

    # ------------------------------------------------------------------ giới hạn dùng
    def _today(self) -> str:
        from bizagent.prompts.engine import today_in

        return today_in(self.ws).isoformat()

    def _check_limits(self, user_id: str, role: str) -> str | None:
        """Thông báo từ chối nếu người này vượt giới hạn tần suất / token trong ngày, ngược lại None."""
        limits = self.ws.agents.limits
        if role in limits.exempt_roles:
            return None
        if limits.per_user_per_minute > 0:
            now = time.monotonic()
            q = self._recent.setdefault(user_id, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limits.per_user_per_minute:
                return (f"Bạn đang hỏi quá nhanh (tối đa {limits.per_user_per_minute} câu mỗi phút). "
                        "Hãy thử lại sau ít phút.")
            q.append(now)
            if len(self._recent) > 5000:                   # dọn người không còn hỏi
                for uid in [u for u, d in self._recent.items() if not d or now - d[-1] > 60]:
                    self._recent.pop(uid, None)
        if limits.daily_tokens_per_user > 0:
            used = self.store.kv_get(f"usage:{self._today()}:{user_id}") or {}
            if used.get("tokens_in", 0) + used.get("tokens_out", 0) >= limits.daily_tokens_per_user:
                return ("Bạn đã dùng hết hạn mức hôm nay. Hạn mức được làm mới vào ngày mai; "
                        "cần thêm hãy liên hệ quản trị viên.")
        return None

    @staticmethod
    def _token_usage(meter: TokenMeter) -> tuple[int, int]:
        """Tổng token vào/ra của MỌI lượt gọi model trong câu hỏi vừa rồi, của mọi agent (router, analyst,
        advisor, designer), kể cả lượt không để lại tin nhắn trong lịch sử (với model trả usage_metadata)."""
        return meter.totals()

    def _thread_lock(self, key: str) -> asyncio.Lock:
        lock = self._thread_locks.get(key)
        if lock is None:
            if len(self._thread_locks) > 2000:             # dọn khoá không ai giữ
                for k in [k for k, v in self._thread_locks.items() if not v.locked()]:
                    del self._thread_locks[k]
            lock = self._thread_locks[key] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------ hỏi đáp
    async def ask(self, text: str, *, user_id: str, role: str | None, thread_id: str) -> str:
        if role is None:
            return "Bạn chưa được cấp quyền dùng trợ lý này. Hãy liên hệ quản trị viên."
        refused = await asyncio.to_thread(self._check_limits, user_id, role)
        if refused:
            self.audit(user_id, "ask", thread=thread_id, outcome="limited", chars=len(text))
            return refused
        graph = await self.ensure_ready()
        key = f"{user_id}:{thread_id}"
        meter = TokenMeter()
        config: RunnableConfig = {
            "configurable": {"thread_id": key, "role": role, "user_id": user_id},
            "recursion_limit": (recursion_limit(self._members) if self.multi_agent
                                else self.settings.max_steps * 2 + 1),
            "callbacks": [meter],
        }
        outcome = "ok"
        state: dict[str, Any] = {}
        # Cùng một cuộc trò chuyện: xử lý lần lượt (hai lượt song song ghi chồng lịch sử hội thoại).
        # Cả tiến trình: tối đa limits.max_concurrent câu cùng lúc, câu sau chờ tới lượt.
        async with self._thread_lock(key), self._slots:
            started = time.monotonic()
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
                outcome = "timeout"
                answer = "Câu hỏi xử lý quá lâu. Hãy thử hỏi ngắn gọn hoặc thu hẹp khoảng thời gian."
            except GraphRecursionError:
                outcome = "too_many_steps"
                answer = "Câu hỏi quá phức tạp (vượt số bước cho phép). Hãy chia nhỏ câu hỏi."
            except Exception as e:
                outcome = "error"
                self.audit(user_id, "ask", thread=thread_id, outcome=outcome, error=str(e)[:500])
                raise
            seconds = round(time.monotonic() - started, 2)
        # Lượt bị timeout: các lần gọi model đã xong vẫn được tính; lần đang chạy dở thì không đo được.
        tokens = self._token_usage(meter)
        await asyncio.to_thread(self.store.touch, user_id, thread_id, text[:80])
        await asyncio.to_thread(self.store.add_usage, self._today(), user_id, *tokens)
        extra: dict[str, Any] = {}
        if self.multi_agent:                            # token theo từng agent + đường router đã chọn
            extra = {"agents": meter.by_agent, "route": state.get("route")}
        self.audit(user_id, "ask", thread=thread_id, outcome=outcome, chars=len(text), seconds=seconds,
                   tokens_in=tokens[0], tokens_out=tokens[1], **extra)
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
        await asyncio.to_thread(self.store.prune_kv, "usage:", 90)          # mức dùng theo ngày giữ 90 ngày
        await asyncio.to_thread(self.store.prune_kv, "login_link:", 2)      # link đăng nhập đã dùng (hạn 24 giờ)
        await asyncio.to_thread(self.store.prune_kv, "alert_", 400)         # trạng thái cảnh báo (kỳ dài nhất: năm)
        return len(victims)

    # ------------------------------------------------------------------ tác vụ nền
    def start_background_tasks(self) -> list[asyncio.Task]:
        """Lịch đồng bộ (sync.yaml, nếu run_in_server) + báo cáo định kỳ (reports.yaml) + cảnh báo theo ngưỡng
        (alerts.yaml) + xuất file Power BI (nếu có powerbi.export) + đẩy dữ liệu lên Power BI Service (nếu có
        powerbi.push) + dọn phiên chat cũ mỗi giờ.
        Gọi trong event loop của server/bot."""
        from bizagent.dashboards.powerbi import PowerBIExporter
        from bizagent.sync import build_runner

        if os.environ.get("BIZAGENT_BACKGROUND_TASKS", "1") == "0":
            # nhiều tiến trình cùng workspace: chỉ MỘT tiến trình chạy tác vụ nền (tránh gửi báo cáo trùng)
            log.info("BIZAGENT_BACKGROUND_TASKS=0: tiến trình này không chạy đồng bộ, báo cáo, cảnh báo, "
                     "xuất/đẩy Power BI")
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
        from bizagent.alerts import AlertRunner

        try:
            self.alert_runner = AlertRunner(self)
            if self.alert_runner.config.alerts:
                tasks.append(asyncio.create_task(self.alert_runner.run_forever()))
        except WorkspaceError as e:
            log.error("Cảnh báo theo ngưỡng lỗi cấu hình: %s", e)
        pbi = self.catalog.config.powerbi if self.catalog else None
        if pbi and pbi.enabled and pbi.export:
            self.powerbi_exporter = PowerBIExporter(self.dashboard_engine)
            tasks.append(asyncio.create_task(self.powerbi_exporter.run_forever()))
        if pbi and pbi.enabled and pbi.push:
            try:
                pusher = self.powerbi_pusher()
                tasks.append(asyncio.create_task(pusher.run_forever()))
            except Exception as e:
                log.error("Đẩy Power BI lỗi cấu hình: %s", e)
        return tasks

    def powerbi_pusher(self):
        """PowerBIPusher dùng chung cho lịch nền và nút "Đẩy ngay" ở /admin (None nếu chưa cấu hình powerbi.push)."""
        from bizagent.dashboards.powerbi_push import PowerBIPusher

        if getattr(self, "_powerbi_pusher", None) is None:
            pbi = self.catalog.config.powerbi if self.catalog else None
            if not pbi or not pbi.enabled or not pbi.push:
                return None
            self._powerbi_pusher = PowerBIPusher(self.dashboard_engine)
        return self._powerbi_pusher

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
        if getattr(self, "_powerbi_pusher", None) is not None:
            self._powerbi_pusher.close()
        self.registry.close()
        self.store.close()
