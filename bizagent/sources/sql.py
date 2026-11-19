"""Nguồn SQL qua SQLAlchemy: PostgreSQL, MySQL/MariaDB, SQL Server, SQLite, Oracle, DuckDB... (có driver là chạy).

Cấu hình trong sources.yaml:

    sales:
      type: sql
      url: "postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST:-localhost}:5432/${DB_NAME}"
      # mysql+pymysql://...   mssql+pyodbc://...?driver=ODBC+Driver+18+for+SQL+Server   sqlite:///${WORKSPACE_DIR}/data/x.db
      description: "Dữ liệu bán hàng"
      allowed_tables: ["dwh.*"]        # bắt buộc: bảng agent được đọc ("schema.table", có *)
      default_schema: dwh              # schema dùng khi SQL không ghi schema
      max_rows: 200
      timeout_seconds: 15
      mask_columns: ["phone", "email"]

Chỉ-đọc gồm 3 lớp: (1) tài khoản DB chỉ có quyền SELECT (bắt buộc, làm ở phía DB);
(2) guard.check_select; (3) thiết lập phiên chỉ-đọc theo dialect + luôn rollback.
Dùng engine đồng bộ chạy trong thread: chạy được mọi driver, không vướng lỗi event loop trên Windows.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from bizagent.security.guard import check_select, to_sqlglot_dialect
from bizagent.security.masking import mask_rows
from bizagent.sources.base import DataSource, QueryResult, SourceError, TableInfo

log = logging.getLogger(__name__)


class SqlSource(DataSource):
    kind = "sql"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        url = config.get("url")
        if not url:
            raise SourceError(f"Nguồn {name}: thiếu 'url'")
        self.allowed_tables: list[str] = list(config.get("allowed_tables") or [])
        if not self.allowed_tables:
            raise SourceError(f"Nguồn {name}: phải khai báo allowed_tables (bảng agent được đọc)")
        self.max_rows = int(config.get("max_rows", 200))
        self.timeout_seconds = int(config.get("timeout_seconds", 15))
        self.mask_columns: list[str] = list(config.get("mask_columns") or [])
        self.engine = self._make_engine(url)
        self._sa_dialect = self.engine.dialect.name
        self.default_schema: str | None = config.get("default_schema") or self._fallback_schema()

    # ------------------------------------------------------------------ kết nối
    def _make_engine(self, url: str) -> Engine:
        u = make_url(url)
        backend = u.get_backend_name()
        ms = self.timeout_seconds * 1000
        connect_args: dict[str, Any] = {}
        if backend == "postgresql":
            connect_args["options"] = f"-c default_transaction_read_only=on -c statement_timeout={ms}"
        elif backend == "sqlite":
            connect_args["check_same_thread"] = False
        engine = create_engine(
            u,
            pool_pre_ping=True,
            pool_size=int(self.config.get("pool_size", 5)),
            connect_args=connect_args,
        )

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # thiết lập phiên chỉ-đọc theo dialect
            try:
                if backend in ("mysql", "mariadb"):
                    cur = dbapi_conn.cursor()
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={ms}")
                    cur.close()
                elif backend == "sqlite":
                    dbapi_conn.execute("PRAGMA query_only = ON")
                elif backend == "mssql" and hasattr(dbapi_conn, "timeout"):
                    dbapi_conn.timeout = self.timeout_seconds   # pyodbc: timeout mỗi câu truy vấn
            except Exception as e:  # không chặn kết nối, nhưng ghi log để quản trị biết
                log.warning("Nguồn %s: không đặt được chế độ chỉ-đọc: %s", self.name, e)

        return engine

    def _fallback_schema(self) -> str | None:
        return {"sqlite": "main", "postgresql": "public", "mssql": "dbo"}.get(self._sa_dialect)

    @property
    def dialect(self) -> str:
        return to_sqlglot_dialect(self._sa_dialect)

    def health(self) -> None:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.rollback()
        except SQLAlchemyError as e:
            raise SourceError(f"Nguồn {self.name}: không kết nối được: {e.__class__.__name__}: {e}") from e

    # ------------------------------------------------------------------ khám phá
    def _schemas(self) -> list[str | None]:
        schemas: list[str | None] = []
        for p in self.allowed_tables:
            if "." in p:
                s = p.split(".", 1)[0]
                if "*" not in s and s not in schemas:
                    schemas.append(s)
        return schemas or [self.default_schema]

    def _allowed(self, full: str) -> bool:
        from bizagent.security.guard import _table_allowed

        return _table_allowed(full, self.allowed_tables)

    def list_tables(self) -> list[str]:
        try:
            insp = inspect(self.engine)
            out: list[str] = []
            for schema in self._schemas():
                names = insp.get_table_names(schema=schema) + insp.get_view_names(schema=schema)
                for n in sorted(set(names)):
                    full = f"{schema}.{n}" if schema else n
                    if self._allowed(full):
                        out.append(full)
            return out
        except SQLAlchemyError as e:
            raise SourceError(f"Nguồn {self.name}: không liệt kê được bảng: {e}") from e

    def describe_table(self, table: str) -> TableInfo:
        schema, _, name = table.rpartition(".")
        schema = schema or self.default_schema
        full = f"{schema}.{name}" if schema else name
        if not self._allowed(full):
            raise SourceError(f"Không có quyền xem bảng {full}")
        try:
            cols = inspect(self.engine).get_columns(name, schema=schema)
        except SQLAlchemyError as e:
            raise SourceError(f"Không đọc được cấu trúc bảng {full}: {e}") from e
        masked = {c.lower() for c in self.mask_columns}
        return TableInfo(
            name=full,
            columns=[
                {
                    "name": c["name"],
                    "type": str(c["type"]),
                    "nullable": c.get("nullable", True),
                    **({"masked": True} if c["name"].lower() in masked else {}),
                }
                for c in cols
            ],
        )

    # ------------------------------------------------------------------ truy vấn
    def query(self, query: str, max_rows: int | None = None) -> QueryResult:
        checked = check_select(
            query, dialect=self.dialect, allowed_tables=self.allowed_tables, default_schema=self.default_schema
        )
        return self._execute(checked, min(max_rows or self.max_rows, self.max_rows))

    def fetch(self, query: str, limit: int) -> QueryResult:
        """Cho đồng bộ về kho: vẫn qua guard và che cột, nhưng giới hạn dòng do job quyết định."""
        checked = check_select(
            query, dialect=self.dialect, allowed_tables=self.allowed_tables, default_schema=self.default_schema
        )
        return self._execute(checked, limit)

    def _execute(self, checked, limit: int) -> QueryResult:
        try:
            with self.engine.connect() as conn:
                try:
                    # exec_driver_sql: không diễn giải ":ten" như text() (chuỗi '10:30' vẫn đúng)
                    res = conn.exec_driver_sql(checked.sql)
                    columns = list(res.keys())
                    rows = [tuple(r) for r in res.fetchmany(limit + 1)]
                finally:
                    conn.rollback()                 # không bao giờ commit
        except SQLAlchemyError as e:
            msg = str(getattr(e, "orig", e)).splitlines()[0][:500]
            raise SourceError(f"Lỗi khi chạy SQL trên {self.name}: {msg}") from e
        truncated = len(rows) > limit
        rows = mask_rows(columns, rows[:limit], self.mask_columns)
        return QueryResult(
            source=self.name, columns=columns, rows=rows, truncated=truncated, meta={"tables": checked.tables}
        )

    def close(self) -> None:
        self.engine.dispose()
