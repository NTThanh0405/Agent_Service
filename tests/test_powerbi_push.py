"""Tự đẩy dữ liệu lên Power BI Service: giả lập Microsoft Entra ID + Power BI REST API bằng httpx.MockTransport."""

import asyncio
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, unquote

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from bizagent.agent.service import AgentService
from bizagent.dashboards.config import DashboardCatalog
from bizagent.dashboards.engine import DashboardEngine
from bizagent.dashboards.powerbi_push import (
    MAX_STRING,
    PowerBIClient,
    PowerBIPusher,
    batches,
    pbi_value,
    read_state,
)
from bizagent.sources.registry import SourceRegistry
from bizagent.sync import build_runner
from bizagent.webauth import hash_password
from bizagent.workspace import UserEntry, WorkspaceError, load_workspace

GROUP = "11111111-2222-3333-4444-555555555555"


class FakePowerBI:
    """Power BI giả: lưu model/bảng/dòng trong bộ nhớ, ghi lại mọi request."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_calls: list[dict] = []
        self.fail: dict[tuple[str, str], list[httpx.Response]] = {}     # (method, đuôi path) -> phản hồi lỗi lần lượt
        self.status_for_all: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "login.microsoftonline.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_calls.append({"path": url.path, **form})
            if form.get("client_secret") != "bi-mat":
                return httpx.Response(401, json={"error": "invalid_client",
                                                 "error_description": "AADSTS7000215: Invalid client secret.\nTrace"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3599, "token_type": "Bearer"})
        assert url.host == "api.powerbi.com"
        assert request.headers["authorization"] == "Bearer tok"
        prefix = f"/v1.0/myorg/groups/{GROUP}"
        path = unquote(url.path)
        assert path.startswith(prefix), path
        tail = path[len(prefix):]
        self.calls.append((request.method, tail))
        if self.status_for_all:
            return httpx.Response(self.status_for_all, json={"error": {"code": "PowerBINotAuthorizedException"}})
        for (m, suffix), queue in self.fail.items():
            if request.method == m and tail.endswith(suffix) and queue:
                return queue.pop(0)
        parts = tail.strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and parts == ["datasets"]:
            return httpx.Response(200, json={"value": [
                {"id": i, "name": m["name"], "addRowsAPIEnabled": m["push"]} for i, m in self.models.items()]})
        if request.method == "POST" and parts == ["datasets"]:
            assert body["defaultMode"] == "Push"
            mid = str(uuid.uuid4())
            self.models[mid] = {"name": body["name"], "push": True, "retention": url.params["defaultRetentionPolicy"],
                                "tables": {t["name"]: {"columns": t["columns"], "rows": []} for t in body["tables"]}}
            return httpx.Response(201, json={"id": mid, "name": body["name"]})
        model = self.models[parts[1]]
        if parts[2:] == ["tables"]:
            return httpx.Response(200, json={"value": [{"name": n, "columns": t["columns"]}
                                                       for n, t in model["tables"].items()]})
        table = model["tables"].get(parts[3])
        if table is None:
            return httpx.Response(404, json={"error": {"code": "ItemNotFound"}})
        if request.method == "PUT":
            table["columns"] = body["columns"]
            return httpx.Response(200, json={"name": parts[3]})
        if request.method == "DELETE":
            table["rows"] = []
            return httpx.Response(200)
        assert len(body["rows"]) <= 10_000
        table["rows"].extend(body["rows"])
        return httpx.Response(200)

    def table(self, name: str, model: str | None = None) -> dict:
        m = next(m for m in self.models.values() if model is None or m["name"] == model)
        return m["tables"][name]

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, t in self.calls if m == method and t.endswith(suffix))


def _write_push(ws_path, **push):
    path = ws_path / "dashboards.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["powerbi"]["push"] = {"workspace_id": GROUP, **push}
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def pbi_env(demo_ws, monkeypatch):
    monkeypatch.setenv("POWERBI_TENANT_ID", "contoso.onmicrosoft.com")
    monkeypatch.setenv("POWERBI_CLIENT_ID", "app-id")
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "bi-mat")
    reg = SourceRegistry(demo_ws)
    runner = build_runner(demo_ws, reg)
    for job in runner.config.jobs:
        runner.run_job(job)
    fake = FakePowerBI()
    sleeps: list[float] = []

    def make(**push) -> PowerBIPusher:
        _write_push(demo_ws.path, **push)
        eng = DashboardEngine(reg, DashboardCatalog(demo_ws))
        return PowerBIPusher(eng, transport=httpx.MockTransport(fake.handler), sleep=sleeps.append)

    yield demo_ws, reg, fake, make, sleeps
    reg.close()


# ----------------------------------------------------------------------------- đẩy
def test_first_push_creates_model_then_replaces_rows(pbi_env):
    ws, reg, fake, make, _ = pbi_env
    pusher = make(datasets=["doanh_thu_ngay", "top_mon"], model="Bán hàng")
    out = pusher.push_all()
    assert all(v.startswith("ok") for v in out.values()), out
    assert fake.count("POST", "/datasets") == 1                          # tạo model đúng một lần
    model = next(iter(fake.models.values()))
    assert model["name"] == "Bán hàng" and model["retention"] == "None"
    assert set(model["tables"]) == {"doanh_thu_ngay", "top_mon"}
    types = {c["name"]: c["dataType"] for c in model["tables"]["top_mon"]["columns"]}
    assert types == {"mon": "String", "nhom": "String", "so_luong": "Int64", "doanh_thu": "Int64"}
    ngay = {c["name"]: c["dataType"] for c in model["tables"]["doanh_thu_ngay"]["columns"]}["ngay"]
    assert ngay == "DateTime"

    # cùng dữ liệu đi qua DashboardEngine như /powerbi
    eng = DashboardEngine(reg, DashboardCatalog(ws))
    from bizagent.dashboards.engine import SYSTEM_ROLE
    from bizagent.dashboards.powerbi import dataset_params
    expected = eng.run_dataset("top_mon", dataset_params(eng, "top_mon", {}), SYSTEM_ROLE, use_cache=False)
    rows = fake.table("top_mon")["rows"]
    assert len(rows) == len(expected.rows) > 0
    assert rows[0] == dict(zip(expected.columns, expected.rows[0], strict=True))
    first_day = fake.table("doanh_thu_ngay")["rows"][0]["ngay"]
    assert datetime.fromisoformat(first_day)                             # ISO datetime

    n = len(rows)
    out2 = pusher.push_all()                                             # lần 2: xoá rồi đẩy lại, không nhân đôi
    assert all(v.startswith("ok") for v in out2.values())
    assert fake.count("POST", "/datasets") == 1 and fake.count("DELETE", "/top_mon/rows") == 1
    assert len(fake.table("top_mon")["rows"]) == n
    assert len(fake.token_calls) == 1                                    # token dùng lại tới khi gần hết hạn
    tok = fake.token_calls[0]
    assert tok["path"] == "/contoso.onmicrosoft.com/oauth2/v2.0/token"
    assert tok["grant_type"] == "client_credentials"
    assert tok["scope"] == "https://analysis.windows.net/powerbi/api/.default"

    state = read_state(ws.path)
    assert state["model"] == "Bán hàng" and state["datasets"] == out2 and state["model_id"] in fake.models
    assert "bi-mat" not in json.dumps(state)


def test_batches_and_append_mode(pbi_env):
    _, _, fake, make, _ = pbi_env
    pusher = make(datasets=["top_mon"], mode="append", batch_rows=4)
    pusher.push_all()
    n = len(fake.table("top_mon")["rows"])
    assert fake.count("POST", "/top_mon/rows") == -(-n // 4)
    pusher.push_all()
    assert len(fake.table("top_mon")["rows"]) == 2 * n                  # append: không xoá dòng cũ
    assert fake.count("DELETE", "/rows") == 0

    rows = [{"a": "x" * 100}] * 50
    parts = list(batches(rows, max_rows=20, max_bytes=1_000))
    assert all(len(p) <= 20 and len(json.dumps(p)) <= 1_000 for p in parts) and sum(map(len, parts)) == 50
    assert [len(p) for p in batches(rows, max_rows=20)] == [20, 20, 10]


def test_default_model_name_period_and_fifo(pbi_env):
    ws, _, fake, make, _ = pbi_env
    pusher = make(retention="basicFIFO", period="7 ngày qua")            # datasets trống = powerbi.datasets
    assert pusher.push_all()
    model = next(iter(fake.models.values()))
    assert model["name"] == f"bizagent {ws.name}" and model["retention"] == "basicFIFO"
    assert set(model["tables"]) == {"doanh_thu_ngay", "muc_tieu_thang", "top_mon"}
    days = {r["ngay"] for r in model["tables"]["doanh_thu_ngay"]["rows"]}
    assert 0 < len(days) <= 7                                            # chỉ 7 ngày, không phải 365


def test_retry_on_429_and_5xx(pbi_env):
    _, _, fake, make, sleeps = pbi_env
    fake.fail[("POST", "/top_mon/rows")] = [httpx.Response(429, headers={"Retry-After": "7"}),
                                            httpx.Response(503)]
    out = make(datasets=["top_mon"]).push_all()
    assert out["top_mon"].startswith("ok") and sleeps[:2] == [7.0, 2]


def test_token_refresh_on_401(pbi_env):
    _, _, fake, make, _ = pbi_env
    fake.fail[("GET", "/datasets")] = [httpx.Response(401)]
    assert make(datasets=["top_mon"]).push_all()["top_mon"].startswith("ok")
    assert len(fake.token_calls) == 2


def test_errors_are_reported_per_dataset(pbi_env, monkeypatch):
    ws, _, fake, make, _ = pbi_env
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "sai")
    out = make(datasets=["top_mon"]).push_all()
    assert "AADSTS7000215" in out["top_mon"] and "sai" not in out["top_mon"] and "Trace" not in out["top_mon"]

    monkeypatch.delenv("POWERBI_CLIENT_SECRET")
    assert "POWERBI_CLIENT_SECRET" in make(datasets=["top_mon"]).push_all()["top_mon"]
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "bi-mat")

    fake.status_for_all = 403
    out = make(datasets=["top_mon"]).push_all()
    assert "HTTP 403" in out["top_mon"] and "Member/Admin" in out["top_mon"]
    fake.status_for_all = None

    fake.models["x"] = {"name": "trung", "push": False, "tables": {}}    # model thường trùng tên
    assert "không phải push model" in make(datasets=["top_mon"], model="trung").push_all()["top_mon"]


def test_new_dataset_after_model_exists_and_schema_update(pbi_env):
    _, _, fake, make, _ = pbi_env
    make(datasets=["top_mon"]).push_all()
    out = make(datasets=["top_mon", "muc_tieu_thang"]).push_all()
    assert out["top_mon"].startswith("ok")
    assert "chưa có bảng muc_tieu_thang" in out["muc_tieu_thang"]

    table = fake.table("top_mon")                                        # Power BI có cột cũ khác -> PUT cập nhật
    table["columns"] = [c for c in table["columns"] if c["name"] != "nhom"]
    assert make(datasets=["top_mon"]).push_all()["top_mon"].startswith("ok")
    assert fake.count("PUT", "/tables/top_mon") == 1
    assert [c["name"] for c in fake.table("top_mon")["columns"]] == ["mon", "nhom", "so_luong", "doanh_thu"]


def test_model_not_created_when_a_dataset_fails(pbi_env):
    ws, _, fake, make, _ = pbi_env
    ds = ws.path / "datasets.yaml"
    raw = yaml.safe_load(ds.read_text(encoding="utf-8"))
    raw["datasets"]["hong"] = {"source": "sales", "sql": "SELECT * FROM bang_khong_co"}
    ds.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    out = make(datasets=["top_mon", "hong"]).push_all()
    assert out["hong"].startswith("lỗi") and out["top_mon"].startswith("chưa đẩy")
    assert fake.models == {}


def test_hourly_row_budget(pbi_env):
    _, _, fake, make, _ = pbi_env
    pusher = make(datasets=["top_mon"])
    pusher.push_all()
    deletes = fake.count("DELETE", "/rows")
    pusher._history.append([pusher._now(), 999_999])
    out = pusher.push_all()
    assert "1,000,000 dòng/giờ" in out["top_mon"]
    assert fake.count("DELETE", "/rows") == deletes                      # không xoá dữ liệu khi không đẩy được


def test_masked_columns_stay_masked(pbi_env):
    ws, _, fake, make, _ = pbi_env
    ds = ws.path / "datasets.yaml"
    raw = yaml.safe_load(ds.read_text(encoding="utf-8"))
    raw["datasets"]["khach"] = {"source": "sales", "sql": "SELECT name, phone, email FROM customers LIMIT 5"}
    ds.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    assert make(datasets=["khach"]).push_all()["khach"].startswith("ok")
    rows = fake.table("khach")["rows"]
    assert rows and all(r["phone"] in (None, "***") and r["email"] in (None, "***") for r in rows)
    assert any(r["phone"] == "***" for r in rows) and all(r["name"] for r in rows)


def test_rate_limit_waits_after_120_posts(monkeypatch):
    monkeypatch.setenv("POWERBI_TENANT_ID", "t")
    monkeypatch.setenv("POWERBI_CLIENT_ID", "c")
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "bi-mat")
    from bizagent.dashboards.config import PowerBIPush

    now = [0.0]
    waits: list[float] = []

    def sleep(s):
        waits.append(s)
        now[0] += s

    client = PowerBIClient(PowerBIPush(workspace_id=GROUP), sleep=sleep, clock=lambda: now[0],
                           transport=httpx.MockTransport(lambda r: httpx.Response(
                               200, json={"access_token": "tok", "expires_in": 3600})))
    for _ in range(121):
        client.post_rows("m", "t", [{"a": 1}])
        now[0] += 0.1
    assert len(waits) == 1 and 47 < waits[0] <= 60


def test_value_conversion():
    assert pbi_value(date(2026, 11, 19), "DateTime") == "2026-11-19T00:00:00"
    assert pbi_value(datetime(2026, 11, 19, 8, 30), "DateTime") == "2026-11-19T08:30:00"
    assert pbi_value(Decimal("12.50"), "Double") == 12.5
    assert pbi_value(float("nan"), "Double") is None
    assert pbi_value(3.0, "Int64") == 3 and pbi_value(3.5, "Int64") is None
    assert pbi_value(True, "Boolean") is True
    assert len(pbi_value("x" * 5000, "String")) == MAX_STRING
    assert pbi_value(date(2026, 1, 2), "String") == "2026-01-02"


def test_config_validation(demo_ws):
    _write_push(demo_ws.path, datasets=["khong_co"])
    with pytest.raises(WorkspaceError, match="powerbi.push.datasets"):
        DashboardCatalog(load_workspace(str(demo_ws.path)))
    path = demo_ws.path / "dashboards.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["powerbi"]["push"] = {"workspace_id": "me"}
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="GUID"):
        DashboardCatalog(load_workspace(str(demo_ws.path)))


# ----------------------------------------------------------------------------- lịch nền, /admin, CLI
async def test_pushed_on_schedule_by_background_task(pbi_env, fake_model_cls, monkeypatch):
    ws, _, fake, make, _ = pbi_env
    make(datasets=["top_mon"], every="1h")
    svc = AgentService(load_workspace(str(ws.path)), model=fake_model_cls(responses=[], seen=[]))
    svc._powerbi_pusher = PowerBIPusher(svc.dashboard_engine, transport=httpx.MockTransport(fake.handler))
    tasks = svc.start_background_tasks()
    try:
        for _ in range(200):
            if fake.models and fake.table("top_mon")["rows"]:
                break
            await asyncio.sleep(0.05)
        assert fake.table("top_mon")["rows"], "lịch nền chưa đẩy dữ liệu"
        assert read_state(ws.path)["datasets"]["top_mon"].startswith("ok")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        svc.close()

    monkeypatch.setenv("BIZAGENT_BACKGROUND_TASKS", "0")
    svc2 = AgentService(load_workspace(str(ws.path)), model=fake_model_cls(responses=[], seen=[]))
    assert svc2.start_background_tasks() == []
    svc2.close()


def test_admin_shows_status_and_push_now(pbi_env, fake_model_cls, monkeypatch):
    from bizagent.channels.rest import create_app

    ws, _, fake, make, _ = pbi_env
    make(datasets=["top_mon"])
    monkeypatch.setenv("BIZAGENT_BACKGROUND_TASKS", "0")
    monkeypatch.setenv("BIZAGENT_SECRET_KEY", "k")
    monkeypatch.delenv("BIZAGENT_API_TOKEN", raising=False)
    ws2 = load_workspace(str(ws.path))
    ws2.users.append(UserEntry(id="web:boss", role="admin", password_hash=hash_password("matkhau123")))
    svc = AgentService(ws2, model=fake_model_cls(responses=[AIMessage(content="ok")], seen=[]))
    svc._powerbi_pusher = PowerBIPusher(svc.dashboard_engine, transport=httpx.MockTransport(fake.handler))
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://testserver"}
    with TestClient(create_app(svc)) as c:
        c.post("/login", content="username=boss&password=matkhau123&next=/admin", headers=form,
               follow_redirects=False)
        page = c.get("/admin").text
        assert "Power BI Service" in page and "chưa đẩy lần nào" in page
        r = c.post("/admin/powerbi/push", headers=form, follow_redirects=False)
        assert r.status_code == 303 and "top_mon" in unquote(r.headers["location"])
        page = c.get("/admin").text
        assert "chưa đẩy lần nào" not in page and "thay toàn bộ" in page
        assert "powerbi_push_manual" in c.get("/admin/audit").text
    assert fake.table("top_mon")["rows"]


def test_cli_push_and_check(pbi_env, monkeypatch, capsys):
    from bizagent import cli

    ws, _, fake, make, _ = pbi_env
    make(datasets=["top_mon"])
    real = PowerBIPusher.client
    monkeypatch.setattr(PowerBIPusher, "client", lambda self: (setattr(self, "_transport", httpx.MockTransport(
        fake.handler)), real(self))[1])
    assert cli.main(["powerbi-push", "-w", str(ws.path), "--check"]) == 0
    assert "tạo ở lần đẩy đầu" in capsys.readouterr().out
    assert cli.main(["powerbi-push", "-w", str(ws.path)]) == 0
    assert "top_mon: ok" in capsys.readouterr().out
    cli.main(["check", "-w", str(ws.path)])
    out = capsys.readouterr().out
    assert "Power BI push (replace, mỗi 1h, 1 dataset): kết nối workspace" in out and "đã có" in out
    monkeypatch.delenv("POWERBI_CLIENT_ID")
    cli.main(["check", "-w", str(ws.path)])
    assert "thiếu POWERBI_CLIENT_ID" in capsys.readouterr().out
