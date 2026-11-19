"""Tool của agent. Tool GENERIC: không gắn với nghiệp vụ hay loại database cụ thể.

Quyền: mỗi tool đọc role của người hỏi từ config["configurable"]["role"] (do AgentService đặt, không đến từ
nội dung tin nhắn) và chỉ cho dùng nguồn mà role đó được xem. Lỗi được trả về dạng chuỗi để agent tự sửa.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import duckdb
import yaml
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from bizagent.dashboards.config import DashboardCatalog
from bizagent.periods import PeriodError, resolve_period
from bizagent.prompts.engine import today_in
from bizagent.security.guard import UnsafeSQLError, check_select
from bizagent.sources.base import DataSource, QueryResult, SourceError
from bizagent.sources.registry import SourceRegistry
from bizagent.sources.rest import RestSource
from bizagent.tabular import SAFE_DUCKDB_CONFIG, create_table
from bizagent.workspace import WorkspaceError

log = logging.getLogger("bizagent.audit")
AuditFn = Callable[..., None]


def _no_audit(*_a, **_k) -> None:
    return None


def build_tools(
    registry: SourceRegistry, catalog: DashboardCatalog | None = None, audit: AuditFn | None = None
) -> list[BaseTool]:
    ws = registry.ws
    audit = audit or _no_audit

    def _source(name: str, config: RunnableConfig) -> DataSource:
        conf = config.get("configurable") or {}
        if name not in ws.sources_for(conf.get("user_id"), conf.get("role")):
            raise SourceError(f"Không có quyền dùng nguồn '{name}' (hoặc nguồn không tồn tại)")
        return registry.get(name)

    @tool
    async def list_sources(config: RunnableConfig) -> str:
        """Liệt kê các nguồn dữ liệu người hỏi được xem (tên, loại, dialect SQL, mô tả)."""
        conf = config.get("configurable") or {}
        items = [
            {"name": s.name, "kind": s.kind, "dialect": s.dialect, "description": s.description}
            for s in registry.for_role(conf.get("role"), conf.get("user_id"))
        ]
        return json.dumps(items, ensure_ascii=False) if items else "Không có nguồn nào được cấp."

    @tool
    async def list_tables(source: str, config: RunnableConfig) -> str:
        """Liệt kê các bảng được phép đọc trong một nguồn SQL, hoặc các operation của một nguồn API (kind=rest)."""
        try:
            src = _source(source, config)
            if isinstance(src, RestSource):
                return json.dumps(src.describe_operations(), ensure_ascii=False)
            tables = await asyncio.to_thread(src.list_tables)
            return "\n".join(tables) or "(không có bảng nào)"
        except SourceError as e:
            return f"Lỗi: {e}"

    @tool
    async def describe_table(source: str, table: str, config: RunnableConfig) -> str:
        """Xem các cột và kiểu dữ liệu của một bảng (schema.table hoặc table), hoặc tham số của một operation API."""
        try:
            src = _source(source, config)
            info = await asyncio.to_thread(src.describe_table, table)
            out: dict = {"table": info.name, "columns": info.columns}
            if info.description:
                out["description"] = info.description
            return json.dumps(out, ensure_ascii=False, default=str)
        except SourceError as e:
            return f"Lỗi: {e}"

    @tool
    async def call_api(
        source: str, operation: str, config: RunnableConfig, params: dict | None = None, sql: str | None = None
    ) -> str:
        """Gọi một operation đã khai báo của nguồn API (kind=rest).
        params: tham số của operation (xem describe_table); khoảng thời gian truyền bằng params.period
        (vd "tháng này") hoặc params.start / params.end (YYYY-MM-DD).
        sql (tuỳ chọn): câu SELECT cú pháp DuckDB chạy trên kết quả, bảng tên `result`, để gộp/lọc trước khi trả về."""
        user = (config.get("configurable") or {}).get("user_id")
        try:
            src = _source(source, config)
            if not isinstance(src, RestSource):
                return f"Lỗi: nguồn {source} không phải nguồn API; dùng run_sql"
            if sql:
                result = await asyncio.to_thread(src.call, operation, params or {}, 100_000)
                result = await asyncio.to_thread(query_result_with_sql, result, sql, src.max_rows)
            else:
                result = await asyncio.to_thread(src.call, operation, params or {})
            log.info("api ok user=%s source=%s op=%s rows=%d", user, source, operation, len(result.rows))
            audit(user, "api", source=source, operation=operation, params=params, rows=len(result.rows))
            return result.to_text()
        except (SourceError, UnsafeSQLError) as e:
            log.info("api lỗi user=%s source=%s op=%s lỗi=%s", user, source, operation, e)
            audit(user, "api_denied", source=source, operation=operation, error=str(e))
            return f"Lỗi: {e}"

    @tool
    async def run_sql(source: str, sql: str, config: RunnableConfig) -> str:
        """Chạy MỘT câu SELECT (chỉ đọc) trên một nguồn SQL, đúng cú pháp dialect của nguồn đó.
        Kết quả bị giới hạn số dòng: hãy GROUP BY / SUM / LIMIT trong SQL."""
        user = (config.get("configurable") or {}).get("user_id")
        try:
            src = _source(source, config)
            if src.kind != "sql":
                return f"Lỗi: nguồn {source} không phải nguồn SQL"
            result = await asyncio.to_thread(src.query, sql)
            log.info("sql ok user=%s source=%s rows=%d sql=%s", user, source, len(result.rows), sql)
            audit(user, "sql", source=source, sql=sql, rows=len(result.rows))
            return result.to_text()
        except (SourceError, UnsafeSQLError) as e:
            log.info("sql bị từ chối user=%s source=%s lỗi=%s sql=%s", user, source, e, sql)
            audit(user, "sql_denied", source=source, sql=sql, error=str(e))
            return f"Lỗi: {e}"

    @tool
    async def get_date_range(expression: str) -> str:
        """Đổi cụm thời gian người dùng nói (vd 'hôm qua', 'tuần này', 'tháng 10/2026', '7 ngày qua',
        'từ 1/11 đến 5/11') thành khoảng ngày cụ thể, kèm kỳ liền trước cùng độ dài để so sánh."""
        try:
            p = resolve_period(expression, today_in(ws), ws.info.week_start)
        except PeriodError as e:
            return f"Lỗi: {e}"
        prev = p.previous()
        return json.dumps(
            {
                "label": p.label,
                "start": p.start.isoformat(),
                "end": p.end.isoformat(),
                "days": p.days,
                "previous_start": prev.start.isoformat(),
                "previous_end": prev.end.isoformat(),
            },
            ensure_ascii=False,
        )

    tools = [list_sources, list_tables, describe_table, run_sql, get_date_range]
    if any(isinstance(s, RestSource) for s in registry.sources.values()):
        tools.append(call_api)            # không có nguồn API thì không gắn tool (đỡ tốn token)
    if catalog is not None and (catalog.config.live.enabled or catalog.config.powerbi.reports):
        tools += build_dashboard_tools(registry, catalog, audit)
    return tools


def build_dashboard_tools(
    registry: SourceRegistry, catalog: DashboardCatalog, audit: AuditFn | None = None
) -> list[BaseTool]:
    audit = audit or _no_audit
    from bizagent.dashboards.engine import DashboardEngine, DashboardError

    engine = DashboardEngine(registry, catalog)

    @tool
    async def list_dashboards(config: RunnableConfig) -> str:
        """Liệt kê dashboard sống và báo cáo Power BI người hỏi xem được, kèm link. Dùng khi người dùng muốn
        xem dashboard/báo cáo/biểu đồ: ưu tiên gửi link dashboard có sẵn trước khi tạo mới."""
        conf = config.get("configurable") or {}
        role = conf.get("role")
        catalog.maybe_reload()
        items = [{"slug": s, "title": catalog.dashboards[s].title, "description": catalog.dashboards[s].description,
                  "link": catalog.link(s), "kind": "live"} for s in catalog.visible(role, conf.get("user_id"))]
        items += [{"title": r.title, "description": r.description, "link": r.url, "kind": "powerbi"}
                  for r in catalog.config.powerbi.reports.values() if not r.roles or role in r.roles]
        note = "" if catalog.config.public_url else " (chưa đặt public_url: link là đường dẫn tương đối)"
        return (json.dumps(items, ensure_ascii=False) + note) if items else "Chưa có dashboard nào."

    @tool
    async def create_dashboard(
        slug: str, title: str, datasets: dict, widgets: list, config: RunnableConfig,
        description: str = "", filters: dict | None = None, refresh_seconds: int = 60,
    ) -> str:
        """Tạo (hoặc sửa dashboard do agent tạo trước đó) một dashboard sống, tự cập nhật, không hết hạn.
        slug: a-z 0-9 _ - . datasets: {ten: {source, sql}}; SQL dùng {{start}} {{end}} {{prev_start}} {{prev_end}}
        của bộ lọc period (mặc định có bộ lọc period = "30 ngày qua").
        widgets: [{type: kpi|line|bar|area|pie|table, title, dataset, x, y: [cot], series, value, compare,
        format: number|currency|percent, width: 1-12}]. Mỗi widget kpi: value (và compare = cột kỳ trước)."""
        conf = config.get("configurable") or {}
        role = conf.get("role")
        if role not in catalog.config.live.agent_can_create:
            return "Lỗi: vai trò của bạn không được tạo dashboard. Hãy nhờ quản trị viên."
        if not catalog.config.live.enabled:
            return "Lỗi: dashboard sống chưa bật (dashboards.yaml: live.enabled)."
        raw = {
            "title": title, "description": description, "refresh_seconds": refresh_seconds,
            "filters": filters or {"period": {"type": "period", "label": "Kỳ", "default": "30 ngày qua"}},
            "datasets": datasets, "widgets": widgets, "created_by": "agent",
        }
        path = catalog.dir / f"{slug}.yaml"
        try:
            if path.exists() and catalog.dashboards.get(slug) and catalog.dashboards[slug].created_by != "agent":
                return f"Lỗi: dashboard '{slug}' do người viết, agent không được ghi đè. Chọn slug khác."
            spec = catalog.parse_dashboard(slug, raw)
            if any(w.type == "custom" for w in spec.widgets):
                return "Lỗi: agent không được tạo widget custom (chỉ người quản trị viết code JS)."
            for ds in spec.datasets.values():
                if ds.operation is not None:
                    return "Lỗi: dataset do agent tạo phải dùng sql."
                if ds.source not in registry.ws.sources_for(conf.get("user_id"), role):
                    return f"Lỗi: không có quyền dùng nguồn {ds.source}"
            params, _ = engine.resolve_filters(spec, {})
            for name in spec.datasets:                       # chạy thử từng dataset (qua guard + quyền)
                r = await asyncio.to_thread(engine.run_dataset, name, params, role, spec=spec, limit=5,
                                            user_id=conf.get("user_id"), use_cache=False)
                for w in spec.widgets:
                    if w.dataset != name:
                        continue
                    need = [c for c in [w.x, w.series, w.value, w.compare, *w.y, *w.columns] if c]
                    missing = [c for c in need if c not in r.columns]
                    if missing:
                        return f"Lỗi: widget '{w.title}' dùng cột không có trong dataset {name}: {missing}. " \
                               f"Cột có: {r.columns}"
        except (WorkspaceError, DashboardError, SourceError, UnsafeSQLError, ValueError) as e:
            return f"Lỗi: {e}"
        catalog.dir.mkdir(parents=True, exist_ok=True)
        header = f"# Dashboard do agent tạo theo yêu cầu của {conf.get('user_id', '?')}. Sửa tay thoải mái;\n" \
                 "# xoá dòng created_by để agent không ghi đè nữa.\n"
        path.write_text(header + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        catalog.reload()
        log.info("dashboard tạo/sửa user=%s slug=%s", conf.get("user_id"), slug)
        audit(conf.get("user_id", "?"), "dashboard_create", slug=slug)
        return f"Đã tạo dashboard '{title}': {catalog.link(slug)} (tự làm mới mỗi {spec.refresh_seconds} giây)"

    return [list_dashboards, create_dashboard]


def query_result_with_sql(result: QueryResult, sql: str, max_rows: int) -> QueryResult:
    """Chạy SELECT (DuckDB) trên kết quả API, bảng tên `result`."""
    checked = check_select(sql, dialect="duckdb", allowed_tables=["main.result"], default_schema="main")
    con = duckdb.connect(":memory:", config=SAFE_DUCKDB_CONFIG)
    try:
        create_table(con, "result", result.columns, result.rows)
        res = con.execute(checked.sql)
        cols = [d[0] for d in res.description]
        rows = res.fetchmany(max_rows + 1)
    except duckdb.Error as e:
        raise SourceError(f"Lỗi SQL trên kết quả API: {str(e).splitlines()[0]}") from e
    finally:
        con.close()
    return QueryResult(source=result.source, columns=cols, rows=rows[:max_rows],
                       truncated=len(rows) > max_rows or result.truncated, meta=result.meta)
