"""Giờ hiển thị theo múi giờ workspace, lỗi dashboard cho mọi role quản trị."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from bizagent.agent.service import AgentService
from bizagent.channels.common import handle_command
from bizagent.channels.rest import create_app


def test_local_time(demo_ws):
    assert demo_ws.info.timezone == "Asia/Ho_Chi_Minh"
    assert demo_ws.local_time(datetime(2026, 11, 20, 0, 30)).hour == 7          # naive = UTC trong DB
    assert demo_ws.local_time(datetime(2026, 11, 20, 18, 0, tzinfo=UTC)).day == 21
    assert demo_ws.local_time(None) is None


async def test_sessions_and_grants_use_workspace_time(demo_ws, fake_model_cls):
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    svc.store.touch("telegram:1", "t1", "doanh thu hôm qua")
    out = await handle_command(svc, "telegram:1", "admin", "/sessions")
    row = svc.store.list_threads("telegram:1")[0]
    assert "UTC" not in out and f"{demo_ws.local_time(row['updated_at']):%d/%m %H:%M}" in out
    svc.store.upsert_user("telegram:2", "staff")
    out = await handle_command(svc, "telegram:1", "admin", "/capquyen 2 sales 5")
    assert "Đã cấp nguồn sales" in out and "UTC" not in out
    assert "UTC" not in await handle_command(svc, "telegram:1", "admin", "/quyen 2")
    await svc.aclose()


def test_dashboard_errors_shown_to_custom_admin_role(demo_ws, fake_model_cls, monkeypatch):
    (demo_ws.path / "dashboards" / "hong.yaml").write_text("title: Hỏng\nwidgets: [{type: bar}]\n", encoding="utf-8")
    demo_ws.info.roles["giamdoc"] = ["*"]
    demo_ws.info.admin_roles = ["giamdoc"]
    monkeypatch.setenv("BIZAGENT_API_TOKEN", "s3cret")
    monkeypatch.setenv("BIZAGENT_API_ROLE", "giamdoc")
    with TestClient(create_app(AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[])))) as c:
        page = c.get("/dashboards", headers={"Authorization": "Bearer s3cret"}).text
    assert "Dashboard lỗi cấu hình" in page and "hong" in page
