"""Chạy dataset cho dashboard sống và Power BI: tham số an toàn, kiểm tra quyền, cache."""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from datetime import time as dtime
from decimal import Decimal
from typing import Any

from sqlglot import exp

from bizagent.dashboards.config import PARAM, DashboardCatalog, DashboardSpec, DatasetSpec
from bizagent.periods import PeriodError, resolve_period
from bizagent.prompts.engine import today_in
from bizagent.sources.base import QueryResult, SourceError
from bizagent.sources.registry import SourceRegistry
from bizagent.sources.rest import RestSource

ALL = "__all__"
SYSTEM_ROLE = "__system__"          # chỉ code nội bộ dùng (xuất file Power BI theo lịch)
_BAD_TEXT = re.compile(r"[\\\x00-\x1f]")
DEFAULT_PERIOD = "30 ngày qua"


class DashboardError(Exception):
    pass


class AccessDenied(DashboardError):
    pass


def jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def sql_literal(value: Any, typ: str, dialect: str) -> str:
    """Giá trị tham số -> literal SQL đã thoát ký tự (không bao giờ ghép chuỗi thô vào SQL)."""
    if value is None:
        return "NULL"
    if typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            try:
                value = float(value) if "." in str(value) else int(str(value))
            except ValueError as e:
                raise DashboardError(f"Giá trị '{value}' không phải số") from e
        return exp.Literal.number(value).sql(dialect=dialect)
    text = str(value)
    if typ == "date":
        try:
            text = date.fromisoformat(text).isoformat()
        except ValueError as e:
            raise DashboardError(f"Ngày không hợp lệ: {value}") from e
    elif len(text) > 200 or _BAD_TEXT.search(text):
        raise DashboardError("Giá trị bộ lọc quá dài hoặc có ký tự không hợp lệ")
    return exp.Literal.string(text).sql(dialect=dialect)


class DashboardEngine:
    def __init__(self, registry: SourceRegistry, catalog: DashboardCatalog):
        self.registry = registry
        self.catalog = catalog
        self.ws = registry.ws
        self._cache: dict[str, tuple[float, QueryResult]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ bộ lọc
    def resolve_filters(self, spec: DashboardSpec, query: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """(tham số cho SQL, trạng thái bộ lọc để hiển thị)."""
        params: dict[str, Any] = {}
        state: dict[str, Any] = {}
        today = today_in(self.ws)
        for name, f in spec.filters.items():
            raw = query.get(name)
            if f.type == "period":
                text = (raw or f.default or DEFAULT_PERIOD).strip()
                try:
                    p = resolve_period(text, today, self.ws.info.week_start)
                except PeriodError as e:
                    raise DashboardError(str(e)) from e
                prev = p.previous()
                prefix = "" if name == "period" else f"{name}_"
                params.update({
                    f"{prefix}start": p.start.isoformat(), f"{prefix}end": p.end.isoformat(),
                    f"{prefix}prev_start": prev.start.isoformat(), f"{prefix}prev_end": prev.end.isoformat(),
                })
                state[name] = {"value": text, "start": p.start.isoformat(), "end": p.end.isoformat(),
                               "prev_start": prev.start.isoformat(), "prev_end": prev.end.isoformat()}
            else:
                value = raw if raw is not None else f.default
                if value in ("", ALL):
                    value = None
                if value is None and not f.allow_all and f.options:
                    value = f.options[0]
                if f.type == "select" and value is not None and f.options and value not in map(str, f.options):
                    raise DashboardError(f"Giá trị bộ lọc {name} không hợp lệ")
                params[name] = value
                state[name] = {"value": value}
        return params, state

    # ------------------------------------------------------------------ dataset
    def render_sql(self, ds: DatasetSpec, params: dict[str, Any], dialect: str) -> str:
        def repl(m: re.Match) -> str:
            key = m.group(1)
            spec = ds.inputs.get(key)
            typ = spec.type if spec else ("date" if key.endswith(("start", "end")) else "string")
            if key in params:
                value = params[key]
            elif spec is not None:
                value = spec.default
            else:
                raise DashboardError(f"Dataset dùng tham số {{{{{key}}}}} nhưng dashboard không có bộ lọc tương ứng")
            return sql_literal(value, typ, dialect)

        return PARAM.sub(repl, ds.sql or "")

    def run_dataset(
        self, name: str, params: dict[str, Any], role: str | None, *, spec: DashboardSpec | None = None,
        limit: int | None = None, use_cache: bool = True, user_id: str | None = None,
    ) -> QueryResult:
        ds = self.catalog.dataset(name, spec)
        if role != SYSTEM_ROLE and ds.source not in self.ws.sources_for(user_id, role):
            raise AccessDenied(f"Không có quyền xem nguồn {ds.source}")
        src = self.registry.get(ds.source)
        limit = limit or ds.max_rows
        if ds.sql is not None:
            if src.kind != "sql":
                raise DashboardError(f"Nguồn {ds.source} không chạy SQL")
            sql = self.render_sql(ds, params, src.dialect or "")
            key = repr((ds.source, sql, limit))
            run = lambda: src.fetch(sql, limit)  # noqa: E731
        else:
            if not isinstance(src, RestSource):
                raise DashboardError(f"Nguồn {ds.source} không phải REST")
            call_params = dict(ds.params)
            if "start" in params and "end" in params:
                call_params.update(start=params["start"], end=params["end"])
            key = repr((ds.source, ds.operation, sorted(call_params.items()), limit))
            run = lambda: src.call(ds.operation, call_params, limit)  # noqa: E731

        ttl = self.catalog.config.live.cache_seconds
        if use_cache:
            with self._lock:
                hit = self._cache.get(key)
                if hit and time.monotonic() - hit[0] < ttl:
                    return hit[1]
        result = run()
        with self._lock:
            self._cache[key] = (time.monotonic(), result)
            if len(self._cache) > 500:
                oldest = sorted(self._cache, key=lambda k: self._cache[k][0])[:100]
                for k in oldest:
                    self._cache.pop(k, None)
        return result

    # ------------------------------------------------------------------ dashboard
    def run_dashboard(self, slug: str, query: dict[str, str], role: str | None,
                      user_id: str | None = None) -> dict[str, Any]:
        spec = self.catalog.dashboards.get(slug)
        if spec is None:
            raise DashboardError(f"Không có dashboard '{slug}'")
        if not self.catalog.can_view(slug, role, user_id):
            raise AccessDenied("Không có quyền xem dashboard này")
        params, filters = self.resolve_filters(spec, query)

        names = list(dict.fromkeys(w.dataset for w in spec.widgets if w.dataset))
        data: dict[str, Any] = {}
        for n in names:
            data[n] = self._result_json(n, params, role, spec, user_id)

        options: dict[str, list[Any]] = {}
        for fname, f in spec.filters.items():
            if f.type != "select":
                continue
            if f.options:
                options[fname] = f.options
            elif f.dataset:
                r = self._result_json(f.dataset, params, role, spec, user_id)
                col = f.column or (r["columns"][0] if r.get("columns") else None)
                idx = r["columns"].index(col) if col in r.get("columns", []) else 0
                options[fname] = [row[idx] for row in r.get("rows", [])]
        return {
            "slug": slug,
            "filters": filters,
            "options": options,
            "data": data,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _result_json(self, name: str, params: dict, role: str | None, spec: DashboardSpec,
                     user_id: str | None = None) -> dict[str, Any]:
        try:
            r = self.run_dataset(name, params, role, spec=spec, user_id=user_id)
        except (SourceError, DashboardError) as e:
            return {"error": str(e)}
        return {
            "columns": r.columns,
            "rows": [[jsonable(v) for v in row] for row in r.rows],
            "truncated": r.truncated,
            "synced_at": r.meta.get("synced_at") or {},
        }
