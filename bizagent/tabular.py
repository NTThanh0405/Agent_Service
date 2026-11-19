"""Tiện ích dữ liệu dạng bảng dùng chung: chuẩn hoá tên, làm phẳng JSON, nạp vào DuckDB."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import duckdb

# DuckDB dùng cho dữ liệu agent đọc: tắt mọi truy cập ra ngoài (file, URL, ATTACH, cài extension).
SAFE_DUCKDB_CONFIG = {"enable_external_access": False}


def slugify(text: str, fallback: str = "col") -> str:
    """'Doanh thu (VNĐ)' -> 'doanh_thu_vnd'. Dùng làm tên bảng/cột an toàn cho SQL."""
    s = str(text).strip().lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        s = fallback
    if s[0].isdigit():
        s = f"{fallback}_{s}"
    return s


def unique_names(names: list[str], fallback: str = "col") -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, n in enumerate(names):
        base = slugify(n, fallback) if n not in (None, "") else f"{fallback}_{i + 1}"
        name = base
        while name in seen:
            seen[base] += 1
            name = f"{base}_{seen[base]}"
        seen.setdefault(name, 1)
        out.append(name)
    return out


def get_path(obj: Any, path: str | None) -> Any:
    """Lấy giá trị theo đường dẫn 'data.items' hoặc 'a.0.b'. None/'' = chính obj."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def flatten(record: Any, max_depth: int = 2) -> dict[str, Any]:
    """{'a': {'b': 1}, 'c': [1,2]} -> {'a.b': 1, 'c': '[1, 2]'}. Mảng và tầng sâu hơn thành chuỗi JSON."""
    if not isinstance(record, dict):
        return {"value": record}
    out: dict[str, Any] = {}

    def walk(prefix: str, value: Any, depth: int) -> None:
        if isinstance(value, dict) and depth < max_depth:
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v, depth + 1)
        elif isinstance(value, (dict, list)):
            out[prefix] = json.dumps(value, ensure_ascii=False, default=str)
        else:
            out[prefix] = value

    walk("", record, 0)
    return out


def records_to_table(records: list[Any], fields: dict[str, str] | None = None) -> tuple[list[str], list[tuple]]:
    """Danh sách bản ghi JSON -> (cột, dòng). `fields` = {tên cột: đường dẫn JSON} để chọn và đổi tên cột."""
    if fields:
        cols = list(fields)
        return cols, [tuple(_scalar(get_path(r, fields[c])) for c in cols) for r in records]
    flat = [flatten(r) for r in records]
    cols: list[str] = []
    for f in flat:
        for k in f:
            if k not in cols:
                cols.append(k)
    return cols, [tuple(f.get(c) for c in cols) for f in flat]


def _scalar(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v


# ----------------------------------------------------------------------------- DuckDB
def infer_type(values: list[Any]) -> str:
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return "VARCHAR"
    if kinds <= {bool}:
        return "BOOLEAN"
    if kinds <= {int}:
        return "BIGINT"
    if kinds <= {int, float, Decimal}:
        return "DOUBLE"
    if kinds <= {datetime}:
        return "TIMESTAMP"
    if kinds <= {date}:
        return "DATE"
    if kinds <= {time}:
        return "TIME"
    return "VARCHAR"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _coerce(v: Any, typ: str) -> Any:
    if v is None:
        return None
    if typ == "VARCHAR" and not isinstance(v, str):
        return json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
    if typ == "DOUBLE" and isinstance(v, Decimal):
        return float(v)
    return v


def create_table(
    con: duckdb.DuckDBPyConnection,
    name: str,
    columns: list[str],
    rows: list[tuple],
    types: dict[str, str] | None = None,
    replace: bool = True,
) -> None:
    """Tạo bảng DuckDB từ (cột, dòng). Kiểu cột tự suy ra, ghi đè bằng `types` (vd {'ngay': 'DATE'})."""
    types = types or {}
    col_types = [types.get(c) or infer_type([r[i] for r in rows]) for i, c in enumerate(columns)]
    ddl = ", ".join(f"{quote_ident(c)} {t}" for c, t in zip(columns, col_types, strict=True))
    con.execute(f"CREATE {'OR REPLACE ' if replace else ''}TABLE {name} ({ddl})")
    if rows:
        placeholders = ", ".join("?" for _ in columns)
        coerced = [tuple(_coerce(v, t) for v, t in zip(r, col_types, strict=True)) for r in rows]
        con.executemany(f"INSERT INTO {name} VALUES ({placeholders})", coerced)
