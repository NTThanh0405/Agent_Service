"""Giao diện chung cho mọi nguồn dữ liệu. Agent và tool chỉ biết giao diện này, không biết DB hay API cụ thể."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class SourceError(Exception):
    """Lỗi khi đọc nguồn (kết nối, timeout...). Thông báo được trả lại cho agent."""


@dataclass
class QueryResult:
    source: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool = False                 # còn dòng nhưng đã cắt ở max_rows
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))
    meta: dict[str, Any] = field(default_factory=dict)

    def to_text(self, max_cell: int = 200) -> str:
        """Dạng bảng gọn (phân cách |) để đưa cho LLM."""
        def cell(v: Any) -> str:
            s = "" if v is None else str(v)
            return s if len(s) <= max_cell else s[: max_cell - 1] + "…"

        lines = [" | ".join(self.columns)]
        lines += [" | ".join(cell(v) for v in r) for r in self.rows]
        tail = f"\n({len(self.rows)} dòng"
        tail += ", ĐÃ CẮT bớt — hãy gộp/lọc trong SQL)" if self.truncated else ")"
        synced = self.meta.get("synced_at")
        if synced:
            tail += "\nDữ liệu kho đồng bộ lúc: " + ", ".join(f"{t} {ts}" for t, ts in synced.items())
        return "\n".join(lines) + tail


@dataclass
class TableInfo:
    name: str                               # "schema.table" hoặc "table"
    columns: list[dict[str, Any]] = field(default_factory=list)   # [{name, type, nullable}]
    description: str = ""


class DataSource(ABC):
    """Một nguồn dữ liệu đã cấu hình (một database, một API...)."""

    kind: str = "generic"                   # sql | rest | ...

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.description: str = config.get("description", "")

    @property
    def dialect(self) -> str | None:
        """Dialect SQL (nếu nguồn chạy SQL). None với nguồn không phải SQL."""
        return None

    @abstractmethod
    def health(self) -> None:
        """Ném SourceError nếu không kết nối được."""

    @abstractmethod
    def list_tables(self) -> list[str]:
        ...

    @abstractmethod
    def describe_table(self, table: str) -> TableInfo:
        ...

    @abstractmethod
    def query(self, query: str, max_rows: int | None = None) -> QueryResult:
        """Chạy truy vấn chỉ-đọc. Với nguồn SQL, `query` là câu SELECT; nguồn khác tự định nghĩa."""

    def fetch(self, query: str, limit: int) -> QueryResult:
        """Như query nhưng giới hạn dòng do người gọi quyết định (đồng bộ, dashboard, Power BI).
        Lớp con nên ghi đè: query() bị giới hạn ở max_rows dành cho agent."""
        return self.query(query, max_rows=limit)

    def close(self) -> None:
        """Tuỳ chọn: giải phóng kết nối."""
