"""Đưa dataset sang Power BI.

Cách 1 — Web connector (khuyên dùng, Power BI Service làm mới theo lịch được):
    Power BI Desktop > Get data > Web > https://<public_url>/powerbi/<dataset>.csv
    Xác thực: Basic — tên + mật khẩu tài khoản web (users.yaml), hoặc tên bất kỳ + mật khẩu = BIZAGENT_API_TOKEN.
    Trang https://<public_url>/powerbi có sẵn mã Power Query (M) đã đặt kiểu cột cho từng dataset.
Cách 2 — Xuất file theo lịch (dashboards.yaml: powerbi.export): Parquet/CSV trong data/powerbi/, Power BI đọc
    bằng connector Folder/Parquet (qua On-premises data gateway nếu dùng Power BI Service).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from bizagent.dashboards.config import DashboardCatalog
from bizagent.dashboards.engine import SYSTEM_ROLE, DashboardEngine, DashboardError, jsonable
from bizagent.periods import PeriodError, resolve_period
from bizagent.prompts.engine import today_in
from bizagent.sources.base import QueryResult
from bizagent.tabular import create_table, infer_type

log = logging.getLogger(__name__)
_EVERY = re.compile(r"^(\d+)\s*([mhd])$")
PQ_TYPES = {
    "BIGINT": "Int64.Type", "DOUBLE": "type number", "BOOLEAN": "type logical", "DATE": "type date",
    "TIMESTAMP": "type datetime", "TIME": "type time", "VARCHAR": "type text",
}


def dataset_params(engine: DashboardEngine, name: str, query: dict[str, str]) -> dict[str, Any]:
    cfg = engine.catalog.config.powerbi
    ds = engine.catalog.dataset(name)
    params: dict[str, Any] = {}
    if ds.sql and ({"start", "end", "prev_start", "prev_end"} & ds.placeholders()):
        try:
            p = resolve_period(query.get("period") or cfg.default_period, today_in(engine.ws),
                               engine.ws.info.week_start)
        except PeriodError as e:
            raise DashboardError(str(e)) from e
        prev = p.previous()
        params.update(start=p.start.isoformat(), end=p.end.isoformat(),
                      prev_start=prev.start.isoformat(), prev_end=prev.end.isoformat())
    for key in ds.inputs:
        if key in query:
            params[key] = query[key]
    return params


def column_types(result: QueryResult) -> list[tuple[str, str]]:
    return [(c, infer_type([r[i] for r in result.rows])) for i, c in enumerate(result.columns)]


def to_csv(result: QueryResult) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(result.columns)
    for row in result.rows:
        w.writerow(["" if v is None else jsonable(v) for v in row])
    return buf.getvalue()


def to_json(result: QueryResult) -> str:
    return json.dumps([{c: jsonable(v) for c, v in zip(result.columns, r, strict=True)} for r in result.rows],
                      ensure_ascii=False)


def m_code(url: str, types: list[tuple[str, str]]) -> str:
    """Mã Power Query (M) để dán vào Advanced Editor."""
    pairs = ", ".join('{{"{}", {}}}'.format(c.replace('"', '""'), PQ_TYPES.get(t, "type text")) for c, t in types)
    return (
        "let\n"
        f'    Source = Csv.Document(Web.Contents("{url}"), [Delimiter=",", Encoding=65001, '
        "QuoteStyle=QuoteStyle.Csv]),\n"
        "    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f'    Typed = Table.TransformColumnTypes(Headers, {{{pairs}}}, "en-US")\n'
        "in\n"
        "    Typed"
    )


class PowerBIExporter:
    """Ghi file Parquet/CSV của các dataset Power BI theo lịch."""

    def __init__(self, engine: DashboardEngine):
        self.engine = engine
        self.catalog: DashboardCatalog = engine.catalog
        cfg = self.catalog.config.powerbi.export
        if cfg is None:
            raise DashboardError("dashboards.yaml chưa có powerbi.export")
        self.cfg = cfg
        m = _EVERY.match(cfg.every)
        if not m:
            raise DashboardError("powerbi.export.every có dạng 30m, 1h, 1d")
        self.interval = timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m[2]]: int(m[1])})
        self.dir = (engine.ws.path / cfg.dir).resolve()
        self.last_run: datetime | None = None
        self.last_result: dict[str, str] = {}

    def export_all(self) -> dict[str, str]:
        self.dir.mkdir(parents=True, exist_ok=True)
        out: dict[str, str] = {}
        for name in self.catalog.powerbi_datasets():
            try:
                params = dataset_params(self.engine, name, {})
                r = self.engine.run_dataset(name, params, SYSTEM_ROLE, limit=self.catalog.config.powerbi.max_rows,
                                            use_cache=False)
                self._write(name, r)
                out[name] = f"ok ({len(r.rows)} dòng{', BỊ CẮT' if r.truncated else ''})"
            except Exception as e:
                log.error("Xuất Power BI %s lỗi: %s", name, e)
                out[name] = f"lỗi: {e}"
        self.last_run = datetime.now(UTC)
        self.last_result = out
        (self.dir / "_export_state.json").write_text(
            json.dumps({"exported_at": self.last_run.isoformat(), "datasets": out}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out

    def _write(self, name: str, r: QueryResult) -> None:
        if "csv" in self.cfg.formats:
            tmp = self.dir / f".{name}.csv.tmp"
            tmp.write_text(to_csv(r), encoding="utf-8-sig")       # BOM: Excel/Power BI nhận đúng tiếng Việt
            tmp.replace(self.dir / f"{name}.csv")
        if "parquet" in self.cfg.formats:
            final = self.dir / f"{name}.parquet"
            tmp = self.dir / f".{name}.parquet.tmp"
            con = duckdb.connect(":memory:")      # DuckDB riêng cho xuất file (không dùng cho agent)
            try:
                create_table(con, "t", r.columns, r.rows)
                path = str(tmp).replace("'", "''")
                con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
            finally:
                con.close()
            tmp.replace(final)                     # thay file một lần: Power BI không đọc phải file đang ghi

    async def run_forever(self) -> None:
        log.info("Xuất dữ liệu Power BI mỗi %s vào %s", self.cfg.every, self.dir)
        while True:
            if self.last_run is None or datetime.now(UTC) - self.last_run >= self.interval:
                try:
                    await asyncio.to_thread(self.export_all)
                except Exception:
                    log.exception("Xuất Power BI lỗi")
            await asyncio.sleep(30)


def ensure_inside(base: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
