"""Che dữ liệu cá nhân TRƯỚC khi tới LLM, log hay dashboard.

Khai báo theo nguồn trong sources.yaml:  mask_columns: ["phone", "email", "*_cccd"]
(so khớp tên cột, không phân biệt hoa thường, có ký tự đại diện *).
Đây là lớp bổ sung; tốt nhất vẫn là tài khoản database không có quyền đọc các cột này.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

MASK = "***"


def masked_indexes(columns: list[str], patterns: list[str]) -> set[int]:
    pats = [p.lower() for p in patterns]
    return {i for i, c in enumerate(columns) if any(fnmatchcase(c.lower(), p) for p in pats)}


def mask_rows(columns: list[str], rows: list[tuple[Any, ...]], patterns: list[str]) -> list[tuple[Any, ...]]:
    idx = masked_indexes(columns, patterns)
    if not idx:
        return rows
    return [tuple(MASK if i in idx and v is not None else v for i, v in enumerate(r)) for r in rows]
