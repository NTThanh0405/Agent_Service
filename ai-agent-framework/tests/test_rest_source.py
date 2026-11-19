import json
from datetime import date

import httpx
import pytest

from bizagent.agent.tools import query_result_with_sql
from bizagent.sources.base import SourceError
from bizagent.sources.rest import RestSource

TODAY = date(2026, 11, 19)


def make(handler, **overrides):
    cfg = {
        "base_url": "https://api.example.com",
        "auth": {"type": "bearer", "value_env": "EX_TOKEN"},
        "rate_limit_per_minute": 0,
        "retries": 1,
        "mask_columns": ["phone"],
        "operations": {
            "orders": {
                "description": "Đơn hàng",
                "method": "GET",
                "path": "/v1/orders",
                "params": {"from": "{start}", "to": "{end}", "page": "{page}", "status": "{status}"},
                "inputs": {"status": {"type": "string", "enum": ["done", "new"]}},
                "result_path": "data.items",
                "fields": {"ma": "id", "tong": "total", "khach": "customer.name", "phone": "customer.phone"},
                "pagination": {"type": "page", "start": 1, "size": 2, "max_pages": 5},
            },
            "summary": {"method": "POST", "path": "/v1/summary", "body_format": "form",
                        "params": {"d1": "{start_dmy}", "d2": "{end_dmy}"}},
        },
    }
    cfg.update(overrides)
    return RestSource("ex", cfg, transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("EX_TOKEN", "tok-123")


def orders_handler(seen):
    data = [{"id": i, "total": i * 1000, "customer": {"name": f"K{i}", "phone": "0901"}} for i in range(1, 6)]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        page = int(req.url.params["page"])
        items = data[(page - 1) * 2: page * 2]
        return httpx.Response(200, json={"data": {"items": items}})

    return handler


def test_pagination_fields_masking_period():
    seen = []
    src = make(orders_handler(seen))
    r = src.call("orders", {"period": "tháng trước", "status": "done"}, today=TODAY)
    assert r.columns == ["ma", "tong", "khach", "phone"]
    assert [row[0] for row in r.rows] == [1, 2, 3, 4, 5]
    assert all(row[3] == "***" for row in r.rows)
    assert len(seen) == 3                                      # trang 3 có 1 bản ghi < size -> dừng
    q = seen[0].url.params
    assert (q["from"], q["to"], q["status"]) == ("2026-10-01", "2026-10-31", "done")
    assert seen[0].headers["Authorization"] == "Bearer tok-123"


def test_missing_placeholder_is_dropped_and_cache():
    seen = []
    src = make(orders_handler(seen))
    src.call("orders", {}, today=TODAY)
    assert "from" not in seen[0].url.params and "status" not in seen[0].url.params
    n = len(seen)
    src.call("orders", {}, today=TODAY)
    assert len(seen) == n                                      # lần 2 lấy cache


def test_truncation():
    src = make(orders_handler([]), max_rows=3)
    r = src.call("orders", {})
    assert len(r.rows) == 3 and r.truncated


@pytest.mark.parametrize(
    "params,msg",
    [
        ({"status": "xoa_het"}, "một trong"),
        ({"status": 5}, "kiểu"),
        ({"url": "https://evil"}, "không được phép"),
        ({"period": "không hiểu"}, "Không hiểu"),
        ({"start": "2020-01-01", "end": "2026-01-01"}, "dài quá"),
    ],
)
def test_input_validation(params, msg):
    with pytest.raises(SourceError, match=msg):
        make(orders_handler([])).call("orders", params, today=TODAY)


def test_unknown_operation():
    with pytest.raises(SourceError, match="không có operation"):
        make(orders_handler([])).call("delete_all", {})


def test_post_form_body_auth(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=[{"a": 1}])

    src = make(handler, auth={"type": "body", "param": "access_token", "value_env": "EX_TOKEN"})
    r = src.call("summary", {"start": "2026-11-01", "end": "2026-11-05"})
    body = dict(x.split("=") for x in seen[0].content.decode().split("&"))
    assert body == {"d1": "01%2F11%2F2026", "d2": "05%2F11%2F2026", "access_token": "tok-123"}
    assert r.rows == [(1,)]


def test_retry_then_success_and_error_hides_url(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, json=[{"x": 1}])

    assert make(flaky).call("summary", {}).rows == [(1,)]

    src = make(lambda req: httpx.Response(401),
               auth={"type": "query", "param": "key", "value_env": "EX_TOKEN"})
    with pytest.raises(SourceError) as e:
        src.call("summary", {})
    assert "HTTP 401" in str(e.value) and "tok-123" not in str(e.value)


def test_missing_secret():
    src = make(orders_handler([]), auth={"type": "bearer", "value_env": "NOT_SET_ANYWHERE"})
    with pytest.raises(SourceError, match="NOT_SET_ANYWHERE"):
        src.health()


def test_bad_config():
    with pytest.raises(SourceError, match="base_url"):
        RestSource("x", {"base_url": "ftp://x", "operations": {"a": {"path": "/"}}})
    with pytest.raises(SourceError, match="GET/POST"):
        RestSource("x", {"base_url": "https://x", "operations": {"a": {"path": "/", "method": "DELETE"}}})


def test_sql_over_api_result():
    r = make(orders_handler([])).call("orders", {})
    out = query_result_with_sql(r, "SELECT SUM(tong) AS t, COUNT(*) AS n FROM result", 10)
    assert out.rows == [(15000, 5)]
    with pytest.raises(Exception, match="quyền"):
        query_result_with_sql(r, "SELECT * FROM other", 10)


def test_describe_lists_inputs_and_period():
    info = make(orders_handler([])).describe_table("orders")
    desc = info.description
    assert "status" in desc and "period" in desc
    assert json.loads(desc.split("Tham số: ")[1])["status"]["enum"] == ["done", "new"]
