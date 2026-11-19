"""Mở dữ liệu của workspace thành MCP server: Claude Desktop, Claude Code, Cursor... hỏi được dữ liệu doanh nghiệp
qua đúng các lớp bảo vệ của bizagent (guard chỉ-SELECT, che cột, quyền theo role, operations API khai báo sẵn).

    bizagent mcp-server -w demo --role manager

Cấu hình Claude Desktop (claude_desktop_config.json):
    {"mcpServers": {"bizagent": {"command": "C:/.../.venv/Scripts/bizagent.exe",
                                 "args": ["mcp-server", "-w", "C:/.../workspaces/demo", "--role", "manager"]}}}

Chạy qua stdio trên máy người dùng; role cố định theo tham số --role. Không cần API key của model AI.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bizagent.dashboards.config import DashboardCatalog
from bizagent.dashboards.engine import DashboardEngine, DashboardError, jsonable
from bizagent.dashboards.powerbi import dataset_params
from bizagent.periods import PeriodError, resolve_period
from bizagent.prompts.engine import load_knowledge, today_in
from bizagent.security.guard import UnsafeSQLError
from bizagent.sources.base import SourceError
from bizagent.sources.registry import SourceRegistry
from bizagent.sources.rest import RestSource
from bizagent.state import StateStore
from bizagent.workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)


class BizagentTools:
    """Logic thuần (không phụ thuộc MCP) để dễ test."""

    def __init__(self, ws: Workspace, role: str):
        if role not in ws.info.roles:
            raise WorkspaceError(f"Role '{role}' không có trong workspace.yaml")
        self.ws, self.role = ws, role
        self.registry = SourceRegistry(ws)
        self.store = StateStore.for_workspace(ws)
        self.store.attach(ws)
        try:
            self.catalog: DashboardCatalog | None = DashboardCatalog(ws)
            self.engine: DashboardEngine | None = DashboardEngine(self.registry, self.catalog)
        except WorkspaceError:
            self.catalog = self.engine = None

    def _src(self, name: str):
        if name not in self.ws.sources_for_role(self.role):
            raise SourceError(f"Không có quyền dùng nguồn '{name}'")
        return self.registry.get(name)

    def _audit(self, kind: str, **detail: Any) -> None:
        self.store.audit(f"mcp:{self.role}", kind, **detail)

    def business_context(self) -> str:
        info = self.ws.info
        head = (f"{info.display_name or info.name}. {info.description}\n"
                f"Tiền tệ {info.currency}, múi giờ {info.timezone}.")
        return head + "\n\n" + load_knowledge(self.ws)

    def list_sources(self) -> str:
        return json.dumps([{"name": s.name, "kind": s.kind, "dialect": s.dialect, "description": s.description}
                           for s in self.registry.for_role(self.role) if s.kind != "mcp"], ensure_ascii=False)

    def list_tables(self, source: str) -> str:
        src = self._src(source)
        if isinstance(src, RestSource):
            return json.dumps(src.describe_operations(), ensure_ascii=False)
        return "\n".join(src.list_tables())

    def describe_table(self, source: str, table: str) -> str:
        info = self._src(source).describe_table(table)
        return json.dumps({"table": info.name, "columns": info.columns, "description": info.description},
                          ensure_ascii=False, default=str)

    def run_sql(self, source: str, sql: str) -> str:
        src = self._src(source)
        if src.kind != "sql":
            raise SourceError(f"Nguồn {source} không phải nguồn SQL")
        try:
            r = src.query(sql)
        except (SourceError, UnsafeSQLError) as e:
            self._audit("sql_denied", source=source, sql=sql, error=str(e))
            raise
        self._audit("sql", source=source, sql=sql, rows=len(r.rows))
        return r.to_text()

    def call_api(self, source: str, operation: str, params: dict | None = None) -> str:
        src = self._src(source)
        if not isinstance(src, RestSource):
            raise SourceError(f"Nguồn {source} không phải API")
        r = src.call(operation, params or {})
        self._audit("api", source=source, operation=operation, rows=len(r.rows))
        return r.to_text()

    def get_date_range(self, expression: str) -> str:
        p = resolve_period(expression, today_in(self.ws), self.ws.info.week_start)
        prev = p.previous()
        return json.dumps({"start": p.start.isoformat(), "end": p.end.isoformat(),
                           "previous_start": prev.start.isoformat(), "previous_end": prev.end.isoformat()})

    def list_datasets(self) -> str:
        if not self.catalog:
            return "[]"
        allowed = set(self.ws.sources_for_role(self.role))
        return json.dumps([{"name": n, "description": d.description, "source": d.source}
                           for n, d in self.catalog.datasets.items() if d.source in allowed], ensure_ascii=False)

    def get_dataset(self, name: str, period: str | None = None, max_rows: int = 500) -> str:
        if not self.engine:
            raise SourceError("Workspace chưa có datasets.yaml")
        params = dataset_params(self.engine, name, {"period": period} if period else {})
        r = self.engine.run_dataset(name, params, self.role, limit=min(max_rows, 5000))
        self._audit("dataset", name=name, rows=len(r.rows))
        return json.dumps({"columns": r.columns, "rows": [[jsonable(v) for v in row] for row in r.rows],
                           "truncated": r.truncated, "synced_at": r.meta.get("synced_at")}, ensure_ascii=False)

    def close(self) -> None:
        self.registry.close()
        self.store.close()


def build_server(ws: Workspace, role: str):
    from mcp.server.fastmcp import FastMCP

    t = BizagentTools(ws, role)
    name = ws.info.display_name or ws.name
    mcp = FastMCP(f"bizagent-{ws.name}", instructions=(
        f"Dữ liệu kinh doanh của {name} (chỉ đọc). Gọi business_context trước để biết định nghĩa chỉ số. "
        "Khoảng thời gian dùng get_date_range. SQL viết đúng dialect của nguồn."))

    def safe(fn, *a, **k) -> str:
        try:
            return fn(*a, **k)
        except (SourceError, UnsafeSQLError, DashboardError, PeriodError, WorkspaceError) as e:
            return f"Lỗi: {e}"

    @mcp.tool()
    def business_context() -> str:
        """Bối cảnh doanh nghiệp và định nghĩa chỉ số (glossary, ví dụ SQL)."""
        return safe(t.business_context)

    @mcp.tool()
    def list_sources() -> str:
        """Các nguồn dữ liệu được xem (tên, loại, dialect SQL, mô tả)."""
        return safe(t.list_sources)

    @mcp.tool()
    def list_tables(source: str) -> str:
        """Bảng được phép đọc của một nguồn SQL, hoặc operation của một nguồn API."""
        return safe(t.list_tables, source)

    @mcp.tool()
    def describe_table(source: str, table: str) -> str:
        """Cột và kiểu dữ liệu của một bảng, hoặc tham số của một operation API."""
        return safe(t.describe_table, source, table)

    @mcp.tool()
    def run_sql(source: str, sql: str) -> str:
        """Chạy MỘT câu SELECT chỉ-đọc trên nguồn SQL (đúng dialect). Kết quả bị giới hạn số dòng."""
        return safe(t.run_sql, source, sql)

    @mcp.tool()
    def call_api(source: str, operation: str, params: dict | None = None) -> str:
        """Gọi operation đã khai báo của nguồn API; params.period = 'tháng này' hoặc start/end."""
        return safe(t.call_api, source, operation, params)

    @mcp.tool()
    def get_date_range(expression: str) -> str:
        """Đổi 'hôm qua', 'tuần này', 'tháng 10/2026', '7 ngày qua'... thành khoảng ngày + kỳ trước."""
        return safe(t.get_date_range, expression)

    @mcp.tool()
    def list_datasets() -> str:
        """Dataset dựng sẵn (cùng số liệu với dashboard)."""
        return safe(t.list_datasets)

    @mcp.tool()
    def get_dataset(name: str, period: str | None = None, max_rows: int = 500) -> str:
        """Lấy dữ liệu một dataset dựng sẵn; period vd 'tháng này' (nếu dataset có tham số ngày)."""
        return safe(t.get_dataset, name, period, max_rows)

    return mcp, t
