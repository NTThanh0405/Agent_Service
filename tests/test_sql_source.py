import pytest

from bizagent.security.guard import UnsafeSQLError
from bizagent.sources.base import SourceError
from bizagent.sources.registry import SourceRegistry


@pytest.fixture
def sales(demo_ws):
    reg = SourceRegistry(demo_ws)
    assert reg.errors == {}
    yield reg.get("sales")
    reg.close()


def test_list_and_describe(sales):
    assert {"main.orders", "main.stores", "main.products"} <= set(sales.list_tables())
    info = sales.describe_table("customers")
    cols = {c["name"]: c for c in info.columns}
    assert cols["phone"].get("masked") is True


def test_query_and_masking(sales):
    r = sales.query("SELECT COUNT(*) AS n FROM orders WHERE status = 'completed'")
    assert r.columns == ["n"] and r.rows[0][0] > 0
    r = sales.query("SELECT name, phone, email FROM customers LIMIT 3")
    assert all(row[1] == "***" and row[2] == "***" for row in r.rows)


@pytest.mark.parametrize("sql", [
    "SELECT name, phone AS p FROM customers",                                   # đổi tên cột
    "SELECT name, phone || '' AS p, lower(email) AS e FROM customers",          # biểu thức
    "SELECT name FROM customers UNION ALL SELECT phone FROM customers",         # UNION
    "SELECT c.phone AS x FROM customers c",                                     # alias bảng
    "WITH t AS (SELECT * FROM customers) SELECT phone AS q FROM t",             # qua CTE
    "SELECT p FROM (SELECT phone AS p FROM customers) s",                       # qua subquery
])
def test_masking_cannot_be_bypassed(sales, sql):
    real = sales.engine.connect().exec_driver_sql("SELECT phone, email FROM customers").fetchall()
    secrets_ = {v for row in real for v in row if v}
    r = sales.query(sql)
    assert r.rows and not ({v for row in r.rows for v in row} & secrets_)


def test_masked_column_cannot_be_probed_in_where(sales):
    real = sales.engine.connect().exec_driver_sql("SELECT phone FROM customers WHERE phone IS NOT NULL").first()[0]
    assert sales.query(f"SELECT COUNT(*) FROM customers WHERE phone = '{real}'").rows[0][0] == 0


def test_truncation(sales):
    r = sales.query("SELECT id FROM orders")
    assert r.truncated and len(r.rows) == sales.max_rows
    assert "ĐÃ CẮT" in r.to_text()


def test_guard_blocks_write(sales):
    with pytest.raises(UnsafeSQLError):
        sales.query("DELETE FROM orders")


def test_session_is_read_only(sales):
    # Lớp 3: kể cả khi lọt guard, phiên SQLite đang ở chế độ query_only
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with sales.engine.connect() as conn, pytest.raises(OperationalError, match="readonly"):
        conn.execute(text("DELETE FROM orders"))


def test_sql_error_is_source_error(sales):
    with pytest.raises(SourceError):
        sales.query("SELECT khong_co_cot FROM orders")


def test_unknown_type_is_isolated(demo_ws):
    from bizagent.workspace import SourceConfig

    demo_ws.sources["api"] = SourceConfig(type="rest")
    reg = SourceRegistry(demo_ws)
    assert "sales" in reg.sources and "api" in reg.errors
    with pytest.raises(SourceError):
        reg.get("api")
    reg.close()
