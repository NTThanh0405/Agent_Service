"""Ổn định: truy vấn bị huỷ thật khi hết giờ, khoá theo cuộc trò chuyện, giới hạn dùng, báo cáo dựng theo nhóm."""

import asyncio
import sqlite3
import threading
import time
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from bizagent.agent.service import AgentService
from bizagent.sources.base import SourceError
from bizagent.sources.sql import SqlSource
from bizagent.sources.warehouse import WarehouseSource
from bizagent.workspace import UserEntry

SLOW_SQL = "SELECT COUNT(*) FROM t a, t b, t c WHERE a.x + b.x + c.x = 7"


# ----------------------------------------------------------------------------- truy vấn bị huỷ khi hết giờ
def test_sqlite_query_is_cancelled_on_timeout(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x INT)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(2000)])
    con.commit()
    con.close()
    src = SqlSource("s", {"url": f"sqlite:///{db.as_posix()}", "allowed_tables": ["main.*"], "timeout_seconds": 1})
    started = time.monotonic()
    with pytest.raises(SourceError, match="quá 1 giây"):
        src.query(SLOW_SQL)
    assert time.monotonic() - started < 5
    assert src.query("SELECT COUNT(*) FROM t").rows == [(2000,)]        # kết nối vẫn dùng tiếp được
    src.close()


def test_warehouse_query_is_interrupted_and_releases_lock(tmp_path):
    wh = WarehouseSource("kho", {"path": str(tmp_path / "w.duckdb"), "timeout_seconds": 1})
    wh.write_table("t", ["x"], [(i,) for i in range(3000)])
    started = time.monotonic()
    with pytest.raises(SourceError, match="quá 1 giây"):
        wh.query(SLOW_SQL)
    assert time.monotonic() - started < 5
    started = time.monotonic()
    assert wh.query("SELECT COUNT(*) FROM t").rows == [(3000,)]          # khoá kho đã được nhả
    assert time.monotonic() - started < 2


# ----------------------------------------------------------------------------- giới hạn dùng
def answer(text: str, t_in: int = 0, t_out: int = 0) -> AIMessage:
    usage = {"input_tokens": t_in, "output_tokens": t_out, "total_tokens": t_in + t_out}
    return AIMessage(content=text, usage_metadata=usage)


async def test_per_user_rate_limit(demo_ws, fake_model_cls):
    demo_ws.agents.limits.per_user_per_minute = 2
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[answer("a"), answer("b"), answer("c")], seen=[]))
    assert await svc.ask("1", user_id="t:1", role="manager", thread_id="x") == "a"
    assert await svc.ask("2", user_id="t:1", role="manager", thread_id="x") == "b"
    assert "quá nhanh" in await svc.ask("3", user_id="t:1", role="manager", thread_id="x")
    assert await svc.ask("3", user_id="t:2", role="manager", thread_id="x") == "c"   # người khác không bị ảnh hưởng
    await svc.aclose()


async def test_daily_token_quota_and_usage_recorded(demo_ws, fake_model_cls):
    demo_ws.agents.limits.daily_tokens_per_user = 1000
    demo_ws.agents.limits.exempt_roles = ["admin"]
    model = fake_model_cls(responses=[answer("a", 700, 400), answer("b", 900, 900)], seen=[])
    svc = AgentService(demo_ws, model=model)
    assert await svc.ask("1", user_id="t:1", role="manager", thread_id="x") == "a"
    used = svc.store.kv_get(f"usage:{svc._today()}:t:1")
    assert used == {"asks": 1, "tokens_in": 700, "tokens_out": 400}
    assert "hết hạn mức" in await svc.ask("2", user_id="t:1", role="manager", thread_id="x")
    assert await svc.ask("2", user_id="t:9", role="admin", thread_id="x") == "b"    # role được miễn
    audit = [r for r in svc.store.audit_rows(10) if r["kind"] == "ask"]
    assert {r["detail"]["outcome"] for r in audit} == {"ok", "limited"}
    assert any(r["detail"].get("tokens_in") == 700 for r in audit)
    await svc.aclose()


# ----------------------------------------------------------------------------- khoá theo cuộc trò chuyện
_active = {"now": 0, "max": 0}
_guard = threading.Lock()


async def test_same_thread_is_serialized_and_concurrency_capped(demo_ws, fake_model_cls):
    class SlowModel(fake_model_cls):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            with _guard:
                _active["now"] += 1
                _active["max"] = max(_active["max"], _active["now"])
            time.sleep(0.3)
            with _guard:
                _active["now"] -= 1
            return super()._generate(messages, stop, run_manager, **kwargs)

    demo_ws.agents.limits.max_concurrent = 2
    svc = AgentService(demo_ws, model=SlowModel(responses=[answer(str(i)) for i in range(6)], seen=[]))
    await svc.ensure_ready()
    _active.update(now=0, max=0)
    await asyncio.gather(*(svc.ask("q", user_id="t:1", role="admin", thread_id="same") for _ in range(3)))
    assert _active["max"] == 1                                            # cùng cuộc trò chuyện: lần lượt
    _active.update(now=0, max=0)
    await asyncio.gather(*(svc.ask("q", user_id=f"t:{i}", role="admin", thread_id="x") for i in range(3)))
    assert _active["max"] == 2                                            # khác cuộc: song song, tối đa 2
    await svc.aclose()


# ----------------------------------------------------------------------------- báo cáo dựng một lần mỗi nhóm quyền
async def test_report_built_once_per_permission_group(demo_ws, fake_model_cls):
    from bizagent.reports import ReportRunner
    from bizagent.sources.registry import SourceRegistry
    from bizagent.sync import build_runner

    reg = SourceRegistry(demo_ws)
    runner = build_runner(demo_ws, reg)
    for job in runner.config.jobs:
        runner.run_job(job)
    reg.close()
    demo_ws.users += [UserEntry(id=f"telegram:{i}", role="manager") for i in (1, 2, 3)]
    demo_ws.users.append(UserEntry(id="telegram:9", role="admin"))
    svc = AgentService(demo_ws, model=fake_model_cls(responses=[], seen=[]))

    class Sender:
        sent: list = []

        async def send(self, chat_id, text):
            self.sent.append(chat_id)

    rr = ReportRunner(svc, senders={"telegram": Sender()})
    builds = []
    real_build = rr.build

    async def counting_build(name, uid, today=None):
        builds.append(uid)
        return await real_build(name, uid, today)

    rr.build = counting_build
    res = await rr.send_now("bao_cao_sang", ["telegram:1", "telegram:2", "telegram:3", "telegram:9"],
                            today=date(2026, 11, 19))
    assert set(res.values()) == {"đã gửi"}
    assert builds == ["telegram:1", "telegram:9"]                         # 2 nhóm quyền -> dựng 2 lần, gửi 4 người
    await svc.aclose()
