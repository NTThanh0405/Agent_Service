"""Kho dữ liệu nội bộ DuckDB: nơi đồng bộ dữ liệu từ API, Google Sheets, file, database khác về.

Có kho rồi agent JOIN chéo nguồn bằng SQL bình thường, dữ liệu lịch sử truy vấn nhanh, và mỗi kết quả cho biết
dữ liệu đồng bộ tới lúc nào (as_of).

    warehouse:
      type: warehouse
      path: "${WORKSPACE_DIR}/data/warehouse.duckdb"
      description: "Kho tổng hợp: dữ liệu đồng bộ từ các nguồn"
      max_rows: 200
      mask_columns: []

DuckDB chỉ cho MỘT tiến trình ghi. Đồng bộ chạy ngay trong tiến trình server/bot (sync.yaml: run_in_server: true);
lệnh `bizagent sync` chỉ dùng khi server không chạy (hoặc sẽ tự thử lại khi file đang bị khoá).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bizagent.security.guard import check_select
from bizagent.security.masking import MaskingError, mask_query, mask_rows
from bizagent.sources.base import DataSource, QueryResult, SourceError, TableInfo
from bizagent.tabular import SAFE_DUCKDB_CONFIG, QueryTimeout, create_table, execute_with_timeout, quote_ident, slugify

META = "bizagent_meta"
_LOCKS: dict[str, threading.Lock] = {}


class WarehouseSource(DataSource):
    kind = "sql"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        if not config.get("path"):
            raise SourceError(f"Nguồn {name}: thiếu path (file .duckdb)")
        self.path = Path(config["path"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = int(config.get("max_rows", 200))
        self.mask_columns: list[str] = list(config.get("mask_columns") or [])
        self.allowed_tables = list(config.get("allowed_tables") or ["main.*"])
        self.timeout_seconds = float(config.get("timeout_seconds", 30))               # câu truy vấn của agent
        self.fetch_timeout_seconds = float(config.get("fetch_timeout_seconds", 300))  # đồng bộ, dashboard, Power BI
        self._lock = _LOCKS.setdefault(str(self.path.resolve()), threading.Lock())
        with self._connect() as con:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {META}")
            con.execute(
                f"""CREATE TABLE IF NOT EXISTS {META}.sync_state (
                    job VARCHAR PRIMARY KEY, table_name VARCHAR, last_run TIMESTAMP, last_success TIMESTAMP,
                    rows BIGINT, status VARCHAR, error VARCHAR)"""
            )

    @property
    def dialect(self) -> str:
        return "duckdb"

    # ------------------------------------------------------------------ kết nối ngắn hạn
    class _Conn:
        def __init__(self, src: WarehouseSource):
            self.src = src

        def __enter__(self) -> duckdb.DuckDBPyConnection:
            self.src._lock.acquire()
            last: Exception | None = None
            for attempt in range(10):                       # tiến trình khác đang giữ file -> thử lại
                try:
                    self.con = duckdb.connect(str(self.src.path), config=SAFE_DUCKDB_CONFIG)
                    return self.con
                except duckdb.IOException as e:
                    last = e
                    time.sleep(0.2 * (attempt + 1))
            self.src._lock.release()
            raise SourceError(f"Kho {self.src.name} đang bị tiến trình khác khoá: {last}")

        def __exit__(self, *exc) -> None:
            try:
                self.con.close()
            finally:
                self.src._lock.release()

    def _connect(self) -> _Conn:
        return self._Conn(self)

    # ------------------------------------------------------------------ đọc (agent)
    def health(self) -> None:
        with self._connect() as con:
            con.execute("SELECT 1")

    def list_tables(self) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema <> ? "
                "ORDER BY 1, 2", [META]
            ).fetchall()
        from bizagent.security.guard import _table_allowed

        return [f"{s}.{t}" for s, t in rows if _table_allowed(f"{s}.{t}", self.allowed_tables)]

    def describe_table(self, table: str) -> TableInfo:
        schema, _, name = table.rpartition(".")
        full = f"{schema or 'main'}.{name}"
        if full not in self.list_tables():
            raise SourceError(f"Không có bảng {full} trong kho")
        with self._connect() as con:
            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position", [schema or "main", name]
            ).fetchall()
            synced = self._synced_at(con, [full])
        desc = f"Đồng bộ lúc {synced[full]}" if full in synced else ""
        return TableInfo(name=full, columns=[{"name": c, "type": t} for c, t in cols], description=desc)

    def _synced_at(self, con: duckdb.DuckDBPyConnection, tables: list[str] | tuple[str, ...]) -> dict[str, str]:
        names = [t.rpartition(".")[2] for t in tables]
        if not names:
            return {}
        rows = con.execute(
            f"SELECT table_name, max(last_success) FROM {META}.sync_state "
            f"WHERE table_name IN ({', '.join('?' for _ in names)}) GROUP BY 1", names
        ).fetchall()
        tz = self.config.get("timezone")
        return {f"main.{t}": _local(ts, tz) for t, ts in rows if ts}

    def query(self, query: str, max_rows: int | None = None) -> QueryResult:
        checked = check_select(query, dialect="duckdb", allowed_tables=self.allowed_tables, default_schema="main")
        limit = min(max_rows or self.max_rows, self.max_rows)
        return self._run(checked.sql, checked.tables, limit, self.timeout_seconds)

    def fetch(self, query: str, limit: int) -> QueryResult:
        """Cho đồng bộ/xuất dữ liệu: qua guard nhưng giới hạn dòng riêng."""
        checked = check_select(query, dialect="duckdb", allowed_tables=self.allowed_tables, default_schema="main")
        return self._run(checked.sql, checked.tables, limit, self.fetch_timeout_seconds)

    def _run(self, sql: str, tables: tuple[str, ...], limit: int, timeout: float) -> QueryResult:
        try:
            with self._connect() as con:
                if self.mask_columns:
                    def columns_of(schema: str | None, table: str) -> list[str]:
                        return [r[0] for r in con.execute(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema = ? "
                            "AND table_name = ? ORDER BY ordinal_position", [schema or "main", table]).fetchall()]

                    try:
                        sql = mask_query(sql, dialect="duckdb", patterns=self.mask_columns, columns_of=columns_of,
                                         default_schema="main")
                    except MaskingError as e:
                        raise SourceError(str(e)) from e
                # hết giờ thì ngắt hẳn: câu chạy lâu không được giữ khoá kho, chặn người khác và lịch đồng bộ
                columns, rows = execute_with_timeout(con, sql, timeout, limit + 1)
                synced = self._synced_at(con, tables)
        except QueryTimeout as e:
            raise SourceError(f"Truy vấn trên kho {self.name} {e} nên đã bị huỷ. Hãy thu hẹp khoảng thời gian "
                              "hoặc gộp dữ liệu (GROUP BY).") from e
        except duckdb.Error as e:
            raise SourceError(f"Lỗi SQL trên {self.name}: {str(e).splitlines()[0]}") from e
        meta: dict[str, Any] = {"tables": tables}
        if synced:
            meta["synced_at"] = synced
        return QueryResult(source=self.name, columns=columns, rows=mask_rows(columns, rows[:limit], self.mask_columns),
                           truncated=len(rows) > limit, meta=meta)

    # ------------------------------------------------------------------ ghi (chỉ đồng bộ dùng)
    def write_table(
        self,
        table: str,
        columns: list[str],
        rows: list[tuple],
        *,
        mode: str = "replace",
        key: list[str] | None = None,
        types: dict[str, str] | None = None,
    ) -> int:
        table = slugify(table, "t")
        t = quote_ident(table)
        stage = quote_ident(f"_stage_{table}")
        with self._connect() as con:
            con.execute("BEGIN")
            try:
                create_table(con, stage, columns, rows, types)
                exists = con.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
                    [table],
                ).fetchone()[0]
                if mode == "replace" or not exists:
                    con.execute(f"CREATE OR REPLACE TABLE {t} AS SELECT * FROM {stage}")
                else:
                    have = [r[0] for r in con.execute(f"DESCRIBE {t}").fetchall()]
                    if have != columns:
                        raise SourceError(f"Bảng {table} có cột {have}, dữ liệu mới có {columns}. "
                                          "Đổi mode thành replace để tạo lại bảng.")
                    if mode == "upsert":
                        if not key:
                            raise SourceError("mode upsert cần khai báo key")
                        cond = " AND ".join(f"{t}.{quote_ident(k)} = s.{quote_ident(k)}" for k in key)
                        con.execute(f"DELETE FROM {t} WHERE EXISTS (SELECT 1 FROM {stage} s WHERE {cond})")
                    elif mode != "append":
                        raise SourceError(f"mode '{mode}' không hợp lệ (replace | append | upsert)")
                    con.execute(f"INSERT INTO {t} SELECT * FROM {stage}")
                con.execute(f"DROP TABLE {stage}")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        return len(rows)

    def record_sync(self, job: str, table: str, *, ok: bool, rows: int = 0, error: str = "") -> None:
        now = datetime.now(UTC).replace(tzinfo=None)          # lưu giờ UTC dạng TIMESTAMP
        with self._connect() as con:
            con.execute(
                f"""INSERT INTO {META}.sync_state VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (job) DO UPDATE SET table_name = excluded.table_name, last_run = excluded.last_run,
                      last_success = CASE WHEN excluded.status = 'ok' THEN excluded.last_run
                                          ELSE {META}.sync_state.last_success END,
                      rows = CASE WHEN excluded.status = 'ok' THEN excluded.rows ELSE {META}.sync_state.rows END,
                      status = excluded.status, error = excluded.error""",
                [job, slugify(table, "t"), now, now if ok else None, rows, "ok" if ok else "error", error[:1000]],
            )

    def sync_state(self) -> dict[str, dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(f"SELECT * FROM {META}.sync_state").fetchall()
        keys = ["job", "table_name", "last_run", "last_success", "rows", "status", "error"]
        out = {}
        for r in rows:
            d = dict(zip(keys, r, strict=True))
            for k in ("last_run", "last_success"):
                if d[k] is not None:
                    d[k] = d[k].replace(tzinfo=UTC)
            out[r[0]] = d
        return out


def _local(ts: datetime, tz: str | None) -> str:
    from zoneinfo import ZoneInfo

    aware = ts.replace(tzinfo=UTC)
    return (aware.astimezone(ZoneInfo(tz)) if tz else aware.astimezone()).strftime("%Y-%m-%d %H:%M")
