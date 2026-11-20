"""Che dữ liệu cá nhân TRƯỚC khi tới LLM, log hay dashboard.

Khai báo theo nguồn trong sources.yaml:  mask_columns: ["phone", "email", "*_cccd"]
(so khớp tên cột, không phân biệt hoa thường, có ký tự đại diện *).
Đây là lớp bổ sung; tốt nhất vẫn là tài khoản database không có quyền đọc các cột này.

Hai lớp:
- mask_query(): với nguồn chạy SQL, mỗi bảng có cột cần che được thay bằng một subquery đã che sẵn, nên mọi cách
  viết (alias `phone AS p`, biểu thức `phone || ''`, UNION, `row_to_json(t)`, WHERE phone LIKE ...) chỉ thấy "***".
  Chỉ che theo tên cột ở KẾT QUẢ là không đủ: đổi tên cột là lộ dữ liệu.
- mask_rows(): che thêm theo tên cột ở kết quả (nguồn bảng nạp vào bộ nhớ, API, và lớp dự phòng).
"""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatchcase
from typing import Any

import sqlglot
from sqlglot import exp

MASK = "***"


class MaskingError(ValueError):
    """Không che được an toàn (vd không biết cột của bảng) -> từ chối truy vấn."""


def is_masked(column: str, patterns: list[str]) -> bool:
    c = column.lower()
    return any(fnmatchcase(c, p.lower()) for p in patterns)


def masked_indexes(columns: list[str], patterns: list[str]) -> set[int]:
    return {i for i, c in enumerate(columns) if is_masked(c, patterns)}


def mask_rows(columns: list[str], rows: list[tuple[Any, ...]], patterns: list[str]) -> list[tuple[Any, ...]]:
    idx = masked_indexes(columns, patterns)
    if not idx:
        return rows
    return [tuple(MASK if i in idx and v is not None else v for i, v in enumerate(r)) for r in rows]


def mask_query(
    sql: str,
    *,
    dialect: str,
    patterns: list[str],
    columns_of: Callable[[str | None, str], list[str]],
    default_schema: str | None = None,
) -> str:
    """Viết lại câu SELECT (đã qua guard) để cột cần che không bao giờ ra khỏi database ở dạng thật.

    `columns_of(schema, table)` trả về danh sách cột của bảng theo đúng thứ tự trong database.
    `FROM kh` -> `FROM (SELECT ten, CASE WHEN phone IS NULL THEN NULL ELSE '***' END AS phone FROM kh) AS kh`.
    """
    if not patterns:
        return sql
    from bizagent.security.guard import _cte_references

    tree = sqlglot.parse_one(sql, read=dialect)
    cte_refs = _cte_references(tree)
    changed = False
    for table in list(tree.find_all(exp.Table)):
        if id(table) in cte_refs:
            continue
        schema = table.db or default_schema
        cols = columns_of(schema or None, table.name)
        if not cols:
            raise MaskingError(f"Không đọc được cột của bảng {table.name} để che dữ liệu cá nhân")
        if not any(is_masked(c, patterns) for c in cols):
            continue
        if table.args.get("pivots") or table.args.get("version") or table.args.get("sample"):
            raise MaskingError(f"Bảng {table.name} có cột bị che: không dùng PIVOT/TABLESAMPLE với bảng này")
        projections = []
        for c in cols:
            ident = exp.to_identifier(c, quoted=True)
            if is_masked(c, patterns):
                masked = exp.Case(
                    ifs=[exp.If(this=exp.Is(this=exp.column(ident.copy()), expression=exp.Null()), true=exp.Null())],
                    default=exp.Literal.string(MASK),
                )
                projections.append(exp.alias_(masked, ident.copy()))
            else:
                projections.append(exp.column(ident))
        base = exp.Table(this=table.this.copy(), db=table.args["db"].copy() if table.args.get("db") else None)
        alias = table.args.get("alias")
        alias = alias.copy() if alias else exp.TableAlias(this=exp.to_identifier(table.name))
        table.replace(exp.Subquery(this=exp.select(*projections).from_(base), alias=alias))
        changed = True
    return tree.sql(dialect=dialect) if changed else sql
