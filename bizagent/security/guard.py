"""Chặn mọi câu SQL không phải SELECT thuần, cho MỌI loại database (theo dialect của sqlglot).

Đây là lớp bảo vệ thứ hai. Lớp thứ nhất (bắt buộc) là tài khoản database chỉ có quyền đọc.

Quy tắc:
- Đúng 1 câu lệnh; gốc là SELECT / UNION / WITH ... SELECT.
- Không có INSERT/UPDATE/DELETE/MERGE/DDL/lệnh không phân tích được ở bất kỳ đâu trong cây.
- Không SELECT ... INTO, không FOR UPDATE.
- Không gọi hàm nguy hiểm (đọc file, sleep, chạy lệnh hệ thống...).
- Mọi bảng phải nằm trong allowlist của nguồn ("schema.table", hỗ trợ ký tự đại diện *).
- Không tham chiếu chéo database (catalog.schema.table), không dùng hàm làm bảng (read_csv(...), OPENROWSET...).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# Tên dialect của SQLAlchemy -> sqlglot
SQLALCHEMY_TO_SQLGLOT = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "tsql",
    "sqlite": "sqlite",
    "oracle": "oracle",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "redshift": "redshift",
    "trino": "trino",
}

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, n)
    for n in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "TruncateTable",
        "Command", "Grant", "Revoke", "Use", "Set", "Transaction", "Commit", "Rollback", "Copy", "LoadData",
        "Pragma", "Attach", "Detach", "Lock",
    )
    if hasattr(exp, n)
)

FORBIDDEN_FUNCTIONS = {
    # PostgreSQL
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export",
    "dblink", "dblink_exec", "pg_terminate_backend", "pg_cancel_backend", "set_config", "current_setting",
    "pg_reload_conf", "query_to_xml",
    # MySQL
    "sleep", "benchmark", "load_file", "get_lock", "release_lock",
    # SQL Server
    "openrowset", "opendatasource", "openquery", "xp_cmdshell", "waitfor",
    # SQLite / DuckDB
    "load_extension", "readfile", "writefile", "read_csv", "read_csv_auto", "read_parquet", "read_json",
    "read_json_auto", "read_ndjson", "read_text", "read_blob", "parquet_scan", "sniff_csv", "glob", "getenv",
    "query", "query_table",
    # Oracle
    "dbms_lock", "utl_file", "utl_http",
    # MySQL UDF hay bị cài để chạy lệnh hệ thống
    "sys_exec", "sys_eval",
}

# Danh sách tên đơn lẻ luôn sót (vd pg_sleep_for, lo_get...): chặn thêm theo tiền tố. Hàm quản trị/hệ thống của các
# database gần như đều mang các tiền tố này, còn hàm phân tích dữ liệu thông thường thì không.
FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_",          # PostgreSQL: pg_sleep_for, pg_sleep_until, pg_read_server_files, pg_ls_waldir...
    "lo_",          # PostgreSQL large object: lo_get, lo_open...
    "dblink",       # PostgreSQL dblink_*
    "xp_", "sp_",   # SQL Server extended / system procedure
    "utl_", "dbms_",  # Oracle package
    "read_",        # DuckDB read_* (đọc file)
)
# Gói Oracle gọi dạng goi.ham(...) (vd UTL_HTTP.REQUEST): chặn mọi định danh mang tiền tố này
FORBIDDEN_IDENTIFIER_PREFIXES = ("utl_", "dbms_")


def _forbidden_function(name: str) -> bool:
    return name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_FUNCTION_PREFIXES)


class UnsafeSQLError(ValueError):
    """Câu SQL bị từ chối. Thông báo được gửi lại cho agent để nó sửa."""


@dataclass(frozen=True)
class CheckedQuery:
    sql: str                 # câu SQL đã chuẩn hoá theo dialect
    tables: tuple[str, ...]  # "schema.table" đã dùng


def to_sqlglot_dialect(sqlalchemy_dialect: str) -> str:
    return SQLALCHEMY_TO_SQLGLOT.get(sqlalchemy_dialect, sqlalchemy_dialect)


def _table_allowed(full_name: str, allowed: list[str]) -> bool:
    name = full_name.lower()
    return any(fnmatchcase(name, p.lower()) for p in allowed)


def _cte_references(tree: exp.Expression) -> set[int]:
    """id() của các nút Table thực sự trỏ tới một CTE (theo phạm vi của sqlglot).

    Không được so tên đơn thuần: `WITH bi_mat AS (SELECT * FROM bi_mat) SELECT * FROM bi_mat` — `bi_mat` bên trong
    là BẢNG THẬT (CTE không đệ quy không tự tham chiếu), nếu coi là CTE thì vượt qua allowlist.
    Nút Table nào không xác định được là CTE thì bị coi là bảng thật và phải qua allowlist (an toàn khi nghi ngờ).
    """
    from sqlglot.optimizer.scope import traverse_scope

    try:
        scopes = traverse_scope(tree)
    except Exception as e:  # phân tích phạm vi lỗi: không đoán, từ chối
        raise UnsafeSQLError(f"Không phân tích được phạm vi bảng trong câu SQL: {str(e).splitlines()[0]}") from e
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    refs: set[int] = set()
    for scope in scopes:
        for table in scope.tables:
            src = scope.sources.get(table.alias_or_name)
            if src is not None and not isinstance(src, exp.Table) and table.name.lower() in cte_names:
                refs.add(id(table))
    return refs


def check_select(
    sql: str,
    *,
    dialect: str,
    allowed_tables: list[str],
    default_schema: str | None = None,
) -> CheckedQuery:
    if not sql or not sql.strip():
        raise UnsafeSQLError("Câu SQL rỗng")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        raise UnsafeSQLError(f"Không phân tích được SQL ({dialect}): {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise UnsafeSQLError("Chỉ được chạy đúng một câu lệnh")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        raise UnsafeSQLError("Chỉ cho phép câu SELECT")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQLError(f"Không được dùng lệnh {node.key.upper()}")
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise UnsafeSQLError("Không được dùng SELECT ... INTO")
        if isinstance(node, exp.Func):
            fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if _forbidden_function(fname):
                raise UnsafeSQLError(f"Không được gọi hàm {fname}")
        if isinstance(node, exp.Identifier) and node.name.lower().startswith(FORBIDDEN_IDENTIFIER_PREFIXES):
            raise UnsafeSQLError(f"Không được dùng {node.name}")

    cte_refs = _cte_references(tree)
    used: list[str] = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeSQLError("Không được dùng hàm hoặc biểu thức làm bảng")
        if table.catalog:
            raise UnsafeSQLError(f"Không được truy vấn chéo database: {table.sql(dialect=dialect)}")
        name = table.name
        schema = table.db
        # DuckDB đọc file khi tên bảng trông như đường dẫn ("data.parquet", 'x/y.csv') -> chặn
        if any(ch in part for part in (name, schema or "") for ch in "./\\:"):
            raise UnsafeSQLError(f"Tên bảng không hợp lệ: {table.sql(dialect=dialect)}")
        if id(table) in cte_refs:
            continue
        schema = schema or default_schema
        full = f"{schema}.{name}" if schema else name
        if not _table_allowed(full, allowed_tables):
            raise UnsafeSQLError(f"Không có quyền truy vấn bảng {full}")
        used.append(full)

    return CheckedQuery(sql=tree.sql(dialect=dialect), tables=tuple(dict.fromkeys(used)))
