"""Nguồn dạng "bảng nạp vào bộ nhớ": Google Sheets, file CSV/Excel...

Agent vẫn viết SQL (dialect duckdb): bảng được tải về (có cache), nạp vào một DuckDB trong bộ nhớ đã tắt truy cập
ra ngoài, rồi chạy câu SELECT đã qua guard. Nhờ vậy mọi nguồn dạng bảng đều hỏi được bằng SQL như database.
"""

from __future__ import annotations

import csv
import threading
import time
from abc import abstractmethod
from pathlib import Path
from typing import Any

import duckdb

from bizagent.security.guard import check_select
from bizagent.security.masking import mask_rows
from bizagent.sources.base import DataSource, QueryResult, SourceError, TableInfo
from bizagent.tabular import (
    SAFE_DUCKDB_CONFIG,
    QueryTimeout,
    create_table,
    execute_with_timeout,
    infer_type,
    slugify,
    unique_names,
)


class TabularSource(DataSource):
    kind = "sql"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.max_rows = int(config.get("max_rows", 200))
        self.timeout_seconds = float(config.get("timeout_seconds", 30))
        self.cache_seconds = int(config.get("cache_seconds", 300))
        self.mask_columns: list[str] = list(config.get("mask_columns") or [])
        self.column_types: dict[str, dict[str, str]] = config.get("column_types") or {}
        self._cache: dict[str, tuple[float, list[str], list[tuple], dict[str, str]]] = {}
        self._lock = threading.Lock()

    @property
    def dialect(self) -> str:
        return "duckdb"

    # --- lớp con cài đặt
    @abstractmethod
    def table_names(self) -> list[str]:
        """Tên bảng (đã slugify)."""

    @abstractmethod
    def fetch_table(self, table: str) -> tuple[list[str], list[tuple]]:
        """Tải một bảng: (tên cột gốc, dòng)."""

    # --- dùng chung
    def load(self, table: str) -> tuple[list[str], list[tuple], dict[str, str]]:
        """(cột đã slugify, dòng, {cột: tên gốc}), có cache."""
        if table not in self.table_names():
            raise SourceError(f"Nguồn {self.name} không có bảng '{table}'. Có: {self.table_names()}")
        with self._lock:
            hit = self._cache.get(table)
            if hit and time.monotonic() - hit[0] < self.cache_seconds:
                return hit[1], hit[2], hit[3]
        raw_cols, rows = self.fetch_table(table)
        cols = unique_names(raw_cols)
        width = len(cols)
        rows = [tuple((list(r) + [None] * width)[:width]) for r in rows]
        rows = [tuple(None if v == "" else v for v in r) for r in rows]
        rows = mask_rows(cols, rows, self.mask_columns)
        original = dict(zip(cols, raw_cols, strict=True))
        with self._lock:
            self._cache[table] = (time.monotonic(), cols, rows, original)
        return cols, rows, original

    def health(self) -> None:
        self.table_names()

    def list_tables(self) -> list[str]:
        return self.table_names()

    def describe_table(self, table: str) -> TableInfo:
        cols, rows, original = self.load(table.rpartition(".")[2])
        types = self.column_types.get(table, {})
        masked = set(self.mask_columns)
        return TableInfo(
            name=table,
            columns=[
                {
                    "name": c,
                    "type": types.get(c) or infer_type([r[i] for r in rows]),
                    "header": original[c],
                    **({"masked": True} if c in masked else {}),
                }
                for i, c in enumerate(cols)
            ],
            description=f"{len(rows)} dòng",
        )

    def query(self, query: str, max_rows: int | None = None) -> QueryResult:
        return self.fetch(query, min(max_rows or self.max_rows, self.max_rows))

    def fetch(self, query: str, limit: int) -> QueryResult:
        names = self.table_names()
        checked = check_select(
            query, dialect="duckdb", allowed_tables=[f"main.{n}" for n in names], default_schema="main"
        )
        con = duckdb.connect(":memory:", config=SAFE_DUCKDB_CONFIG)
        try:
            for full in checked.tables:
                t = full.rpartition(".")[2]
                cols, rows, _ = self.load(t)
                create_table(con, t, cols, rows, self.column_types.get(t))
            columns, out = execute_with_timeout(con, checked.sql, self.timeout_seconds, limit + 1)
        except QueryTimeout as e:
            raise SourceError(f"Truy vấn trên {self.name} {e} nên đã bị huỷ") from e
        except duckdb.Error as e:
            raise SourceError(f"Lỗi SQL trên {self.name}: {str(e).splitlines()[0]}") from e
        finally:
            con.close()
        return QueryResult(source=self.name, columns=columns, rows=out[:limit], truncated=len(out) > limit,
                           meta={"tables": checked.tables})

    def fetch_all(self, table: str) -> tuple[list[str], list[tuple]]:
        """Cho đồng bộ: toàn bộ bảng (đã che cột)."""
        cols, rows, _ = self.load(table)
        return cols, rows


class FilesSource(TabularSource):
    """Thư mục file CSV / Excel (.xlsx cần openpyxl). Mỗi file CSV là một bảng; mỗi sheet Excel là một bảng
    tên <file>__<sheet>.

        ke_hoach:
          type: files
          path: "${WORKSPACE_DIR}/files"
          csv: {delimiter: ",", encoding: "utf-8-sig"}
    """

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.path = Path(config.get("path", ""))
        if not self.path.is_dir():
            raise SourceError(f"Nguồn {name}: không thấy thư mục {self.path}")
        opts = config.get("csv") or {}
        self.delimiter = opts.get("delimiter", ",")
        self.encoding = opts.get("encoding", "utf-8-sig")
        self._index: dict[str, tuple[Path, str | None]] | None = None

    def _scan(self) -> dict[str, tuple[Path, str | None]]:
        index: dict[str, tuple[Path, str | None]] = {}
        for f in sorted(self.path.iterdir()):
            if f.suffix.lower() == ".csv":
                index[slugify(f.stem, "t")] = (f, None)
            elif f.suffix.lower() in (".xlsx", ".xlsm"):
                for sheet in self._excel_sheets(f):
                    index[f"{slugify(f.stem, 't')}__{slugify(sheet, 's')}"] = (f, sheet)
        return index

    def table_names(self) -> list[str]:
        if self._index is None or self.cache_seconds == 0:
            self._index = self._scan()
        return list(self._index)

    @staticmethod
    def _excel_sheets(f: Path) -> list[str]:
        try:
            import openpyxl
        except ImportError as e:
            raise SourceError('Đọc Excel cần openpyxl: pip install "bizagent[excel]"') from e
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()

    def fetch_table(self, table: str) -> tuple[list[str], list[tuple]]:
        f, sheet = self._index[table]
        if sheet is None:
            with f.open(encoding=self.encoding, newline="") as fh:
                rows = list(csv.reader(fh, delimiter=self.delimiter))
            rows = [[_number(v) for v in r] for r in rows]
        else:
            import openpyxl

            wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
            try:
                rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
            finally:
                wb.close()
        rows = [r for r in rows if any(v not in (None, "") for v in r)]
        if not rows:
            return [], []
        return [str(h) if h is not None else "" for h in rows[0]], [tuple(r) for r in rows[1:]]


def _number(v: str) -> Any:
    """CSV đọc ra chuỗi: đổi '123' -> 123, '1.5' -> 1.5; giữ nguyên chuỗi khác (kể cả '0123')."""
    s = v.strip()
    if not s or (len(s) > 1 and s[0] == "0" and s[1].isdigit()):
        return s
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s
