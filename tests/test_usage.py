"""Báo cáo mức dùng token: chỉ trả khi người dùng gõ lệnh, không nằm trong báo cáo định kỳ."""

from datetime import date, timedelta

from bizagent.agent.service import AgentService
from bizagent.channels.common import handle_command
from bizagent.cli import main
from bizagent.reports import ReportRunner


def _seed(svc):
    today = date.fromisoformat(svc._today())
    s = svc.store
    s.add_usage(today.isoformat(), "telegram:1", 1000, 200)
    s.add_usage((today - timedelta(days=3)).isoformat(), "telegram:1", 500, 100)
    s.add_usage((today - timedelta(days=20)).isoformat(), "telegram:1", 4000, 1000)
    s.add_usage((today - timedelta(days=45)).isoformat(), "telegram:1", 9_000_000, 9)      # ngoài 30 ngày
    s.add_usage(today.isoformat(), "telegram:2", 50, 5)
    s.add_usage(today.isoformat(), "report:bao_cao_sang", 1200, 80)


async def test_token_command_own_usage(demo_ws, fake_model_cls):
    demo_ws.agents.limits.daily_tokens_per_user = 5000
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    _seed(svc)
    out = await handle_command(svc, "telegram:1", "staff", "/token")
    assert "Mức dùng token của telegram:1" in out
    assert "Hôm nay: 1 lượt · vào 1.000 · ra 200 · tổng 1.200" in out
    assert "7 ngày: 2 lượt" in out and "tổng 1.800" in out
    assert "30 ngày: 3 lượt" in out and "tổng 6.800" in out and "9.000" not in out
    assert "hôm nay còn 3.800" in out
    # người thường không xem được người khác / toàn bộ
    assert "chỉ xem được mức dùng của chính mình" in await handle_command(svc, "telegram:1", "staff", "/token all")
    assert "/token" in await handle_command(svc, "telegram:1", "staff", "/help")
    await svc.aclose()


async def test_token_command_admin(demo_ws, fake_model_cls):
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    _seed(svc)
    out = await handle_command(svc, "telegram:9", "admin", "/token all")
    assert "Tổng mức dùng token" in out and "report:bao_cao_sang" in out
    assert out.index("telegram:1") < out.index("telegram:2")                    # xếp theo dùng nhiều
    assert "Mức dùng token của telegram:2" in await handle_command(svc, "telegram:9", "admin", "/token 2")
    assert "1.280" in await handle_command(svc, "telegram:9", "admin", "/mucdung report:bao_cao_sang")
    await svc.aclose()


async def test_usage_not_in_periodic_report(demo_ws, fake_model_cls):
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    svc.store.upsert_user("telegram:10", "manager")
    _seed(svc)
    text = await ReportRunner(svc).build("bao_cao_sang", "telegram:10", today=date(2026, 11, 19))
    assert "Báo cáo sáng" in text and "token" not in text.lower()
    await svc.aclose()


def test_cli_usage(demo_ws, fake_model_cls, capsys):
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))
    _seed(svc)
    svc.close()
    assert main(["usage", "-w", str(demo_ws.path)]) == 0
    out = capsys.readouterr().out
    assert "telegram:1" in out and "report:bao_cao_sang" in out
    assert main(["usage", "-w", str(demo_ws.path), "--user", "telegram:2"]) == 0
    assert "Mức dùng token của telegram:2" in capsys.readouterr().out
