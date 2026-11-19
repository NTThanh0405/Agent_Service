"""Đồng bộ dữ liệu các nguồn về kho DuckDB theo lịch (workspaces/<ten>/sync.yaml).

    warehouse: warehouse          # tên nguồn type: warehouse trong sources.yaml
    run_in_server: true           # serve / telegram tự chạy lịch đồng bộ
    jobs:
      don_hang_ngay:              # từ nguồn SQL
        from: {source: sales, sql: "SELECT order_date, SUM(total_amount) AS doanh_thu FROM orders GROUP BY 1"}
        table: don_hang_ngay
        mode: replace             # replace | append | upsert
        every: 1h                 # 30m, 1h, 1d ... hoặc at: "06:00" (mỗi ngày lúc 6h)
      de_xuat_base:               # từ REST API: mỗi lần lấy 30 ngày gần nhất, ghi đè theo id
        from: {source: base_vn, operation: list_requests, params: {}, window_days: 30}
        table: base_de_xuat
        mode: upsert
        key: [id]
        every: 2h
      muc_tieu:                   # từ Google Sheets / file
        from: {source: ke_hoach, table: muc_tieu}
        table: muc_tieu
        mode: replace
        every: 1d
        types: {thang: DATE}      # tuỳ chọn: ép kiểu cột
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bizagent.sources.base import SourceError
from bizagent.sources.registry import SourceRegistry
from bizagent.sources.rest import RestSource
from bizagent.sources.sql import SqlSource
from bizagent.sources.tabular import TabularSource
from bizagent.sources.warehouse import WarehouseSource
from bizagent.workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)
_INTERVAL = re.compile(r"^(\d+)\s*([mhd])$")


class JobFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    sql: str | None = None
    operation: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    window_days: int | None = None
    table: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> JobFrom:
        if sum(x is not None for x in (self.sql, self.operation, self.table)) != 1:
            raise ValueError("from: cần đúng một trong sql / operation / table")
        return self


class SyncJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: JobFrom = Field(alias="from")
    table: str
    mode: str = "replace"
    key: list[str] = Field(default_factory=list)
    every: str | None = None
    at: str | None = None
    max_rows: int = 1_000_000
    types: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> SyncJob:
        if self.mode not in ("replace", "append", "upsert"):
            raise ValueError("mode phải là replace | append | upsert")
        if self.mode == "upsert" and not self.key:
            raise ValueError("mode upsert cần key")
        if self.every and not _INTERVAL.match(self.every):
            raise ValueError("every có dạng 30m, 2h, 1d")
        if self.at and not re.fullmatch(r"\d{1,2}:\d{2}", self.at):
            raise ValueError('at có dạng "06:00"')
        return self

    def interval(self) -> timedelta | None:
        if not self.every:
            return None
        n, unit = _INTERVAL.match(self.every).groups()
        return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: int(n)})


class SyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warehouse: str = "warehouse"
    run_in_server: bool = True
    check_every_seconds: int = 30
    jobs: dict[str, SyncJob] = Field(default_factory=dict)


def load_sync_config(ws: Workspace) -> SyncConfig | None:
    path = ws.path / "sync.yaml"
    if not path.exists():
        return None
    try:
        return SyncConfig(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    except Exception as e:
        raise WorkspaceError(f"sync.yaml không hợp lệ: {e}") from e


class SyncRunner:
    def __init__(self, ws: Workspace, registry: SourceRegistry, config: SyncConfig):
        self.ws = ws
        self.registry = registry
        self.config = config
        wh = registry.get(config.warehouse)
        if not isinstance(wh, WarehouseSource):
            raise WorkspaceError(f"sync.yaml: nguồn '{config.warehouse}' phải có type: warehouse")
        self.warehouse = wh
        for name, job in config.jobs.items():
            if job.from_.source not in ws.sources:
                raise WorkspaceError(f"sync.yaml: job {name} lấy từ nguồn không tồn tại '{job.from_.source}'")

    # ------------------------------------------------------------------ một job
    def _extract(self, job: SyncJob, today: date) -> tuple[list[str], list[tuple]]:
        src = self.registry.get(job.from_.source)
        f = job.from_
        if f.sql is not None:
            if isinstance(src, (SqlSource, WarehouseSource)):
                res = src.fetch(f.sql, job.max_rows)
            else:
                raise SourceError(f"Nguồn {f.source} không chạy được SQL cho đồng bộ")
            if res.truncated:
                raise SourceError(f"Vượt max_rows={job.max_rows}; tăng max_rows của job hoặc gộp dữ liệu trong SQL")
            return res.columns, res.rows
        if f.operation is not None:
            if not isinstance(src, RestSource):
                raise SourceError(f"Nguồn {f.source} không phải REST")
            params = dict(f.params)
            if f.window_days:
                params.update(start=(today - timedelta(days=f.window_days - 1)).isoformat(), end=today.isoformat())
            res = src.call(f.operation, params, max_rows=job.max_rows, today=today)
            if res.truncated:
                raise SourceError("Kết quả API bị cắt (max_rows/max_pages): tăng giới hạn hoặc thu hẹp window_days")
            return res.columns, res.rows
        if not isinstance(src, TabularSource):
            raise SourceError(f"Nguồn {f.source} không có bảng để đồng bộ (dùng sql hoặc operation)")
        return src.fetch_all(f.table)

    def run_job(self, name: str) -> int:
        job = self.config.jobs[name]
        today = datetime.now(ZoneInfo(self.ws.info.timezone)).date()
        try:
            cols, rows = self._extract(job, today)
            n = self.warehouse.write_table(job.table, cols, rows, mode=job.mode, key=job.key, types=job.types)
        except Exception as e:
            log.error("Đồng bộ %s lỗi: %s", name, e)
            self.warehouse.record_sync(name, job.table, ok=False, error=str(e))
            raise
        self.warehouse.record_sync(name, job.table, ok=True, rows=n)
        log.info("Đồng bộ %s: %d dòng -> %s", name, n, job.table)
        return n

    # ------------------------------------------------------------------ lịch
    def due_jobs(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        local = now.astimezone(ZoneInfo(self.ws.info.timezone))
        state = self.warehouse.sync_state()
        due = []
        for name, job in self.config.jobs.items():
            if not job.enabled:
                continue
            last = (state.get(name) or {}).get("last_run")
            if job.at:
                h, m = map(int, job.at.split(":"))
                slot = local.replace(hour=h, minute=m, second=0, microsecond=0)
                if local >= slot and (last is None or last < slot):
                    due.append(name)
            elif job.interval():
                if last is None or now - last >= job.interval():
                    due.append(name)
        return due

    async def run_due(self) -> dict[str, str]:
        results: dict[str, str] = {}
        for name in self.due_jobs():
            try:
                n = await asyncio.to_thread(self.run_job, name)
                results[name] = f"ok ({n} dòng)"
            except Exception as e:
                results[name] = f"lỗi: {e}"
        return results

    async def run_forever(self) -> None:
        log.info("Lịch đồng bộ đang chạy: %s", list(self.config.jobs))
        while True:
            try:
                await self.run_due()
            except Exception:
                log.exception("Vòng đồng bộ lỗi")
            await asyncio.sleep(self.config.check_every_seconds)


def build_runner(ws: Workspace, registry: SourceRegistry) -> SyncRunner | None:
    cfg = load_sync_config(ws)
    if cfg is None or not cfg.jobs:
        return None
    return SyncRunner(ws, registry, cfg)
