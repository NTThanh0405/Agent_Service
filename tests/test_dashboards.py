import base64
import json
import time

import duckdb
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from bizagent.agent.service import AgentService
from bizagent.agent.tools import build_dashboard_tools
from bizagent.dashboards.config import DashboardCatalog
from bizagent.dashboards.engine import AccessDenied, DashboardEngine, DashboardError, sql_literal
from bizagent.dashboards.powerbi import PowerBIExporter, m_code
from bizagent.sources.registry import SourceRegistry
from bizagent.sync import build_runner
from bizagent.webauth import Signer, WebAuth, hash_password, verify_password
from bizagent.workspace import UserEntry


@pytest.fixture
def env(demo_ws):
    reg = SourceRegistry(demo_ws)
    runner = build_runner(demo_ws, reg)
    for job in runner.config.jobs:
        runner.run_job(job)
    cat = DashboardCatalog(demo_ws)
    yield demo_ws, reg, cat, DashboardEngine(reg, cat)
    reg.close()


# ----------------------------------------------------------------------------- tham số SQL an toàn
def test_sql_literal_escaping():
    assert sql_literal("O'Brien", "string", "postgres") == "'O''Brien'"
    assert sql_literal("x' OR 1=1 --", "string", "tsql") == "'x'' OR 1=1 --'"
    assert sql_literal(None, "string", "mysql") == "NULL"
    assert sql_literal("12.5", "number", "sqlite") == "12.5"
    assert sql_literal("2026-11-01", "date", "duckdb") == "'2026-11-01'"
    for bad, typ in (("a\\b", "string"), ("x" * 201, "string"), ("1; DROP", "number"), ("2026-13-01", "date")):
        with pytest.raises(DashboardError):
            sql_literal(bad, typ, "mysql")


# ----------------------------------------------------------------------------- catalog + engine
def test_catalog_roles(env):
    ws, _, cat, _ = env
    assert cat.errors == {} and list(cat.dashboards) == ["tong_quan"]
    assert cat.visible("admin") == ["tong_quan"] and cat.visible("manager") == ["tong_quan"]
    assert cat.visible("staff") == [] and cat.visible(None) == []
    assert set(cat.powerbi_datasets()) == {"doanh_thu_ngay", "muc_tieu_thang", "top_mon"}


def test_run_dashboard_with_filters(env):
    _, _, _, eng = env
    all_ = eng.run_dashboard("tong_quan", {}, "manager")
    one = eng.run_dashboard("tong_quan", {"cua_hang": "Chi nhánh Quận 1", "period": "tuần trước"}, "manager")
    assert all(not d.get("error") for d in all_["data"].values())
    assert one["filters"]["period"]["start"] == "2026-11-09"
    total = lambda r: r["data"]["kpi"]["rows"][0][0]  # noqa: E731
    assert 0 < total(one) < total(all_)
    assert len(all_["options"]["cua_hang"]) == 3
    assert all_["data"]["dat_muc_tieu"]["synced_at"]                      # dữ liệu kho có giờ đồng bộ
    with pytest.raises(DashboardError):
        eng.run_dashboard("tong_quan", {"period": "không hiểu"}, "manager")
    with pytest.raises(AccessDenied):
        eng.run_dashboard("tong_quan", {}, "staff")


def test_injection_in_filter_is_literal(env):
    _, _, _, eng = env
    r = eng.run_dashboard("tong_quan", {"cua_hang": "x' OR '1'='1"}, "manager")
    assert r["data"]["kpi"]["rows"][0][0] is None                         # không khớp cửa hàng nào


def test_hot_reload_and_isolated_errors(env):
    ws, _, cat, _ = env
    (cat.dir / "loi.yaml").write_text("title: Lỗi\nwidgets: [{type: line, dataset: khong_co, x: a, y: [b]}]\n",
                                      encoding="utf-8")
    (cat.dir / "moi.yaml").write_text(
        "title: Mới\nwidgets: [{type: table, dataset: top_mon}]\nfilters: {period: {type: period}}\n", encoding="utf-8")
    time.sleep(0.01)
    cat.maybe_reload()
    assert "moi" in cat.dashboards and "tong_quan" in cat.dashboards
    assert "khong_co" in cat.errors["loi"]


# ----------------------------------------------------------------------------- Power BI
def test_powerbi_export_and_m_code(env):
    ws, _, _, eng = env
    out = PowerBIExporter(eng).export_all()
    assert all(v.startswith("ok") for v in out.values())
    d = ws.path / "data" / "powerbi"
    n = duckdb.connect().execute(f"SELECT COUNT(*) FROM '{(d / 'doanh_thu_ngay.parquet').as_posix()}'").fetchone()[0]
    assert n > 0
    assert (d / "top_mon.csv").read_text(encoding="utf-8-sig").startswith("mon,nhom,so_luong,doanh_thu")
    code = m_code("https://x/powerbi/a.csv", [("ngay", "DATE"), ("tien", "BIGINT"), ('t"en', "VARCHAR")])
    assert '{"ngay", type date}' in code and '{"t""en", type text}' in code


# ----------------------------------------------------------------------------- xác thực
def test_password_and_signer():
    h = hash_password("matkhau123")
    assert verify_password("matkhau123", h) and not verify_password("sai", h)
    with pytest.raises(ValueError):
        hash_password("ngan")
    s = Signer("k")
    t = s.sign({"u": "a"}, 60)
    assert s.verify(t)["u"] == "a"
    assert s.verify(t[:-2] + "xx") is None and Signer("khac").verify(t) is None
    assert s.verify(s.sign({"u": "a"}, -1)) is None


def test_authenticate_paths(demo_ws, monkeypatch):
    monkeypatch.setenv("BIZAGENT_API_TOKEN", "tok")
    monkeypatch.setenv("BIZAGENT_SECRET_KEY", "secret")
    demo_ws.users.append(UserEntry(id="web:lan", role="manager", password_hash=hash_password("matkhau123")))
    a = WebAuth(demo_ws)
    basic = lambda u, p: "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()  # noqa: E731
    kw = {"cookie": None, "client_host": "10.0.0.5"}
    assert a.authenticate(authorization="", **kw) is None                       # máy khác, không xác thực
    assert a.authenticate(authorization="Bearer tok", **kw).role == "admin"
    assert a.authenticate(authorization="Bearer sai", **kw) is None
    assert a.authenticate(authorization=basic("powerbi", "tok"), **kw).user_id == "api:token"
    assert a.authenticate(authorization=basic("lan", "matkhau123"), **kw).role == "manager"
    assert a.authenticate(authorization=basic("lan", "sai"), **kw) is None
    cookie = a.session_token("web:lan")
    assert a.authenticate(cookie=cookie, authorization="", client_host="10.0.0.5").user_id == "web:lan"
    link = a.login_link_token("web:lan")
    assert a.exchange_link(link) and a.exchange_link(cookie) is None             # cookie phiên không dùng làm link
    demo_ws.users.pop()                                                          # xoá người dùng -> mất quyền ngay
    assert a.authenticate(cookie=cookie, authorization="", client_host="10.0.0.5") is None


# ----------------------------------------------------------------------------- web
@pytest.fixture
def client(env, fake_model_cls, monkeypatch):
    ws, *_ = env
    monkeypatch.setenv("BIZAGENT_SECRET_KEY", "secret")
    monkeypatch.delenv("BIZAGENT_API_TOKEN", raising=False)
    ws.users.append(UserEntry(id="web:lan", role="manager", password_hash=hash_password("matkhau123")))
    svc = AgentService(ws, model=fake_model_cls(responses=[AIMessage(content="ok")], seen=[]))
    from bizagent.channels.rest import create_app

    with TestClient(create_app(svc)) as c:
        yield c
    svc.close()


def test_web_login_dashboard_and_powerbi(client):
    assert client.get("/api/v1/dashboards/tong_quan/data").status_code == 401
    assert client.get("/d/tong_quan", follow_redirects=False).status_code == 303
    r = client.post("/login", content="username=lan&password=sai&next=/d/tong_quan",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    r = client.post("/login", content="username=lan&password=matkhau123&next=//evil.com",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.headers["location"] == "/dashboards"                                # chặn chuyển hướng ra ngoài
    page = client.get("/d/tong_quan")
    assert page.status_code == 200 and "Content-Security-Policy" in page.headers
    spec = client.get("/api/v1/dashboards/tong_quan").json()
    assert "datasets" not in spec and "SELECT" not in json.dumps(spec)          # không lộ SQL
    data = client.get("/api/v1/dashboards/tong_quan/data", params={"period": "tháng này"}).json()
    assert data["data"]["kpi"]["rows"][0][0] > 0
    assert client.get("/api/v1/dashboards/tong_quan/script/tien_do_muc_tieu.js").status_code == 200
    assert client.get("/api/v1/dashboards/tong_quan/script/..%2Fsources.yaml").status_code == 404
    assert client.get("/powerbi/top_mon.csv").text.lstrip("﻿").startswith("mon,")
    assert client.get("/powerbi/kpi.csv").status_code == 404                     # không mở cho Power BI
    assert "TransformColumnTypes" in client.get("/powerbi").text


def test_powerbi_basic_challenge(client):
    client.cookies.clear()
    r = client.get("/powerbi/top_mon.csv", headers={"Authorization": "Basic " + base64.b64encode(b"x:y").decode()})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    ok = client.get("/powerbi/top_mon.json",
                    headers={"Authorization": "Basic " + base64.b64encode(b"lan:matkhau123").decode()})
    assert ok.status_code == 200 and ok.json()[0]["mon"]


# ----------------------------------------------------------------------------- tool của agent
async def test_agent_create_dashboard(env):
    ws, reg, cat, _ = env
    tools = {t.name: t for t in build_dashboard_tools(reg, cat)}
    cfg = {"configurable": {"role": "admin", "user_id": "t:1"}}
    good = {
        "slug": "theo_kenh", "title": "Theo kênh",
        "datasets": {"k": {"source": "sales", "sql": "SELECT channel AS kenh, SUM(total_amount) AS dt FROM orders "
                                                     "WHERE order_date BETWEEN {{start}} AND {{end}} GROUP BY 1"}},
        "widgets": [{"type": "bar", "title": "DT", "dataset": "k", "x": "kenh", "y": ["dt"]}],
    }
    out = await tools["create_dashboard"].ainvoke(good, config=cfg)
    assert "Đã tạo" in out and "/d/theo_kenh" in out and "theo_kenh" in cat.dashboards
    assert cat.dashboards["theo_kenh"].created_by == "agent"

    bad_col = {**good, "widgets": [{"type": "bar", "dataset": "k", "x": "kenh", "y": ["khong_co"]}]}
    assert "khong_co" in await tools["create_dashboard"].ainvoke(bad_col, config=cfg)
    bad_sql = {**good, "datasets": {"k": {"source": "sales", "sql": "DELETE FROM orders"}}}
    assert "Lỗi" in await tools["create_dashboard"].ainvoke(bad_sql, config=cfg)
    custom = {**good, "widgets": [{"type": "custom", "script": "tien_do_muc_tieu.js"}]}
    assert "custom" in await tools["create_dashboard"].ainvoke(custom, config=cfg)
    human = {**good, "slug": "tong_quan"}
    assert "ghi đè" in await tools["create_dashboard"].ainvoke(human, config=cfg)
    mgr = {"configurable": {"role": "manager", "user_id": "t:2"}}
    assert "không được tạo" in await tools["create_dashboard"].ainvoke(good, config=mgr)

    listed = json.loads((await tools["list_dashboards"].ainvoke({}, config=mgr)).split(" (chưa")[0])
    assert {d["slug"] for d in listed} == {"tong_quan", "theo_kenh"}
