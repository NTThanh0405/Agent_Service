from datetime import UTC, datetime, timedelta

import pytest

from bizagent.sources.base import SourceError
from bizagent.sources.registry import SourceRegistry
from bizagent.sync import SyncRunner, build_runner, load_sync_config


@pytest.fixture
def runner(demo_ws):
    reg = SourceRegistry(demo_ws)
    assert reg.errors == {}
    r = build_runner(demo_ws, reg)
    yield r
    reg.close()


def test_demo_jobs_and_cross_source_join(runner):
    assert runner.run_job("doanh_thu_ngay") > 0
    assert runner.run_job("muc_tieu_thang") == 27
    wh = runner.warehouse
    assert wh.list_tables() == ["main.doanh_thu_ngay", "main.muc_tieu_thang"]   # bảng meta bị ẩn
    r = wh.query(
        """SELECT m.cua_hang, SUM(d.doanh_thu) AS thuc_te, MAX(m.muc_tieu_doanh_thu_vnd) AS muc_tieu
           FROM muc_tieu_thang m JOIN doanh_thu_ngay d
             ON d.ma_cua_hang = m.ma_cua_hang AND strftime(d.ngay, '%Y-%m') = m.thang
           WHERE m.thang = '2026-11' GROUP BY 1"""
    )
    assert len(r.rows) == 3 and all(row[1] > 0 for row in r.rows)
    assert "đồng bộ lúc" in r.to_text()
    types = {c["name"]: c["type"] for c in wh.describe_table("doanh_thu_ngay").columns}
    assert types["ngay"] == "DATE"
    state = wh.sync_state()
    assert state["doanh_thu_ngay"]["status"] == "ok"


def test_warehouse_is_read_only_for_agent(runner):
    runner.run_job("muc_tieu_thang")
    for bad in ("DELETE FROM muc_tieu_thang", "SELECT * FROM bizagent_meta.sync_state",
                "SELECT * FROM read_csv('x.csv')", "ATTACH 'x.db'"):
        with pytest.raises(Exception):  # noqa: B017 - guard hoặc lỗi quyền đều được
            runner.warehouse.query(bad)


def test_warehouse_masking_cannot_be_bypassed(tmp_path):
    from bizagent.sources.warehouse import WarehouseSource

    wh = WarehouseSource("kho", {"path": str(tmp_path / "w.duckdb"), "mask_columns": ["phone"]})
    wh.write_table("kh", ["ten", "phone"], [("A", "0901234567"), ("B", None)])
    for sql in ("SELECT ten, phone AS p FROM kh", "SELECT phone || '' AS z FROM kh",
                "SELECT ten FROM kh UNION SELECT phone FROM kh", "SELECT * FROM kh"):
        values = {v for row in wh.query(sql).rows for v in row}
        assert "0901234567" not in values and ("***" in values)
    assert wh.query("SELECT COUNT(*) FROM kh WHERE phone LIKE '09%'").rows == [(0,)]
    assert wh.query("SELECT COUNT(phone) FROM kh").rows == [(1,)]            # NULL vẫn là NULL


def test_upsert_append_and_schema_check(runner):
    wh = runner.warehouse
    wh.write_table("t", ["id", "v"], [(1, "a"), (2, "b")])
    wh.write_table("t", ["id", "v"], [(2, "B"), (3, "c")], mode="upsert", key=["id"])
    assert wh.fetch("SELECT id, v FROM t ORDER BY id", 10).rows == [(1, "a"), (2, "B"), (3, "c")]
    wh.write_table("t", ["id", "v"], [(4, "d")], mode="append")
    assert wh.fetch("SELECT COUNT(*) FROM t", 10).rows == [(4,)]
    with pytest.raises(SourceError, match="replace"):
        wh.write_table("t", ["id", "khac"], [(5, "e")], mode="append")
    assert wh.fetch("SELECT COUNT(*) FROM t", 10).rows == [(4,)]          # lỗi thì giữ nguyên dữ liệu cũ


def test_failed_job_keeps_old_data(runner, demo_ws):
    runner.run_job("muc_tieu_thang")
    (demo_ws.path / "files" / "muc_tieu.csv").unlink()
    runner.registry.get("ke_hoach")._index = None
    with pytest.raises(SourceError):
        runner.run_job("muc_tieu_thang")
    s = runner.warehouse.sync_state()["muc_tieu_thang"]
    assert s["status"] == "error" and s["last_success"] is not None and s["rows"] == 27
    assert runner.warehouse.fetch("SELECT COUNT(*) FROM muc_tieu_thang", 10).rows == [(27,)]


def test_due_jobs(runner):
    assert set(runner.due_jobs()) == {"doanh_thu_ngay", "muc_tieu_thang"}     # chưa chạy lần nào
    runner.run_job("doanh_thu_ngay")
    runner.run_job("muc_tieu_thang")
    now = datetime.now(UTC)
    assert runner.due_jobs(now) == []
    assert runner.due_jobs(now + timedelta(minutes=31)) == ["muc_tieu_thang"]
    assert set(runner.due_jobs(now + timedelta(hours=2))) == {"doanh_thu_ngay", "muc_tieu_thang"}


async def test_run_due_reports(runner):
    results = await runner.run_due()
    assert all(v.startswith("ok") for v in results.values()) and len(results) == 2


def test_config_validation(demo_ws):
    (demo_ws.path / "sync.yaml").write_text(
        "jobs:\n  x:\n    from: {source: sales, sql: 'SELECT 1', table: t}\n    table: x\n", encoding="utf-8"
    )
    with pytest.raises(Exception, match="đúng một"):
        load_sync_config(demo_ws)
    (demo_ws.path / "sync.yaml").write_text(
        "jobs:\n  x:\n    from: {source: sales, sql: 'SELECT 1'}\n    table: x\n    mode: upsert\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="key"):
        load_sync_config(demo_ws)
    (demo_ws.path / "sync.yaml").write_text(
        "warehouse: sales\njobs:\n  x:\n    from: {source: sales, sql: 'SELECT 1'}\n    table: x\n",
        encoding="utf-8",
    )
    reg = SourceRegistry(demo_ws)
    with pytest.raises(Exception, match="warehouse"):
        SyncRunner(demo_ws, reg, load_sync_config(demo_ws))
    reg.close()
