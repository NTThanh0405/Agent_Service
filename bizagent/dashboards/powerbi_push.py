"""Tự đẩy dữ liệu lên Power BI Service (push semantic model qua Power BI REST API).

    dashboards.yaml:
      powerbi:
        enabled: true
        datasets: [doanh_thu_ngay, top_mon]
        push: {every: 1h, workspace_id: "<GUID workspace>", model: "bizagent", mode: replace}
    .env:  POWERBI_TENANT_ID, POWERBI_CLIENT_ID, POWERBI_CLIENT_SECRET   (service principal Microsoft Entra ID)

Mỗi dataset trong datasets.yaml thành MỘT BẢNG trong một semantic model kiểu Push trên workspace Power BI.
Lần đầu chưa có model thì tạo (kiểu cột suy từ dữ liệu). Mỗi lần đẩy:
  replace: xoá mọi dòng của bảng rồi đẩy lại toàn bộ kết quả dataset;
  append : chỉ thêm dòng (hợp với dataset lấy dữ liệu mới, vd period: "hôm qua" và every: 1d).
Dữ liệu chạy qua DashboardEngine.run_dataset như dashboard và /powerbi: guard chỉ-SELECT + che cột của nguồn.

API (learn.microsoft.com/rest/api/power-bi/push-datasets), base https://api.powerbi.com/v1.0/myorg:
  GET    /groups/{g}/datasets                                     tìm model theo tên
  POST   /groups/{g}/datasets?defaultRetentionPolicy=None|basicFIFO   tạo model (defaultMode: Push)
  GET    /groups/{g}/datasets/{id}/tables                         schema bảng hiện có
  PUT    /groups/{g}/datasets/{id}/tables/{bang}                  cập nhật cột
  DELETE /groups/{g}/datasets/{id}/tables/{bang}/rows             xoá mọi dòng
  POST   /groups/{g}/datasets/{id}/tables/{bang}/rows             thêm dòng {"rows": [...]}
Giới hạn push semantic model (learn.microsoft.com/power-bi/developer/embedded/push-datasets-limitations):
  10.000 dòng mỗi request POST rows, 1.000.000 dòng thêm vào mỗi giờ mỗi model, 120 request POST rows mỗi phút,
  75 bảng, 75 cột, 5.000.000 dòng mỗi bảng (retention None) / 200.000 (basicFIFO), chuỗi tối đa 4.000 ký tự.
Microsoft ngừng cho TẠO MỚI push semantic model sau 31/10/2027 (model đã tạo vẫn dùng được).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from bizagent.dashboards.config import DashboardCatalog, PowerBIPush
from bizagent.dashboards.engine import SYSTEM_ROLE, DashboardEngine, DashboardError
from bizagent.dashboards.powerbi import dataset_params, parse_every
from bizagent.sources.base import QueryResult
from bizagent.tabular import infer_type

log = logging.getLogger(__name__)

ENV_VARS = ("POWERBI_TENANT_ID", "POWERBI_CLIENT_ID", "POWERBI_CLIENT_SECRET")
TOKEN_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
MAX_ROWS_PER_POST = 10_000
MAX_BYTES_PER_POST = 15 * 1024 * 1024        # Power BI nhận tối đa 16 MB mỗi request
POSTS_PER_MINUTE = 120
ROWS_PER_HOUR = 1_000_000
MAX_COLUMNS = 75
MAX_STRING = 4_000
MAX_TABLE_ROWS = {"none": 5_000_000, "basicFIFO": 200_000}
PBI_TYPES = {
    "BIGINT": "Int64", "DOUBLE": "Double", "BOOLEAN": "Boolean", "DATE": "DateTime", "TIMESTAMP": "DateTime",
    "TIME": "String", "VARCHAR": "String",
}
STATE_FILE = "_push_state.json"


class PowerBIPushError(DashboardError):
    pass


def missing_env() -> list[str]:
    return [n for n in ENV_VARS if not os.environ.get(n)]


def model_name(cfg: PowerBIPush, workspace_name: str) -> str:
    return cfg.model.strip() or f"bizagent {workspace_name}"


# ----------------------------------------------------------------------------- schema + giá trị
def table_schema(name: str, result: QueryResult, remote: dict[str, str] | None = None) -> dict[str, Any]:
    """Bảng Power BI từ kết quả dataset. Cột toàn NULL thì giữ kiểu đang có trên Power BI (nếu có)."""
    if len(result.columns) > MAX_COLUMNS:
        raise PowerBIPushError(f"có {len(result.columns)} cột, Power BI cho tối đa {MAX_COLUMNS}")
    lower = [c.lower() for c in result.columns]
    if len(set(lower)) != len(lower):
        raise PowerBIPushError("trùng tên cột (Power BI không phân biệt hoa thường); đặt AS khác nhau trong SQL")
    remote_l = {k.lower(): v for k, v in (remote or {}).items()}
    cols = []
    for i, c in enumerate(result.columns):
        values = [r[i] for r in result.rows]
        typ = PBI_TYPES.get(infer_type(values), "String")
        if all(v is None for v in values) and c.lower() in remote_l:
            typ = remote_l[c.lower()]
        cols.append({"name": c, "dataType": typ})
    return {"name": name, "columns": cols}


def _same_schema(table: dict[str, Any], remote: dict[str, str]) -> bool:
    mine = [(c["name"].lower(), c["dataType"].lower()) for c in table["columns"]]
    theirs = [(k.lower(), v.lower()) for k, v in remote.items()]
    return mine == theirs


def pbi_value(v: Any, typ: str) -> Any:
    """Giá trị Python -> giá trị JSON đúng kiểu cột Power BI."""
    if v is None:
        return None
    t = typ.lower()
    if t == "string":
        if isinstance(v, (datetime, date, dtime)):
            v = v.isoformat()
        elif isinstance(v, bytes):
            v = v.hex()
        return str(v)[:MAX_STRING]
    if t == "datetime":
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, date):
            return f"{v.isoformat()}T00:00:00"
        return str(v)
    if t == "boolean":
        return bool(v)
    if t in ("int64", "double", "decimal"):
        if isinstance(v, bool):
            return int(v)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        if t == "int64":
            return int(v) if isinstance(v, int) else (int(f) if f.is_integer() else None)
        return float(v) if isinstance(v, Decimal) else f
    return v


def to_rows(result: QueryResult, table: dict[str, Any]) -> list[dict[str, Any]]:
    cols = [(c["name"], c["dataType"]) for c in table["columns"]]
    return [{name: pbi_value(v, typ) for (name, typ), v in zip(cols, row, strict=True)} for row in result.rows]


def batches(rows: list[dict[str, Any]], max_rows: int = MAX_ROWS_PER_POST,
            max_bytes: int = MAX_BYTES_PER_POST) -> Iterator[list[dict[str, Any]]]:
    """Chia dòng thành lô: không quá max_rows dòng và không quá max_bytes byte JSON mỗi lô."""
    batch: list[dict[str, Any]] = []
    size = 20
    for row in rows:
        n = len(json.dumps(row, ensure_ascii=False).encode()) + 1
        if batch and (len(batch) >= max_rows or size + n > max_bytes):
            yield batch
            batch, size = [], 20
        batch.append(row)
        size += n
    if batch:
        yield batch


# ----------------------------------------------------------------------------- REST client
class PowerBIClient:
    """Gọi Power BI REST API bằng service principal (client credentials). Không in secret ra log/lỗi."""

    def __init__(self, cfg: PowerBIPush, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        missing = missing_env()
        if missing:
            raise PowerBIPushError(f"thiếu biến môi trường {', '.join(missing)} (.env)")
        self.cfg = cfg
        self.tenant, self.client_id, self.secret = (os.environ[n] for n in ENV_VARS)
        self.http = httpx.Client(timeout=cfg.timeout_seconds, transport=transport)
        self.base = f"{cfg.api_url.rstrip('/')}/v1.0/myorg/groups/{cfg.workspace_id}"
        self._sleep, self._clock = sleep, clock
        self._token: str | None = None
        self._token_until = 0.0
        self._posts: deque[float] = deque()

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------ token
    def token(self) -> str:
        if self._token and self._clock() < self._token_until:
            return self._token
        url = f"{self.cfg.authority.rstrip('/')}/{quote(self.tenant, safe='')}/oauth2/v2.0/token"
        try:
            r = self.http.post(url, data={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.secret, "scope": TOKEN_SCOPE,
            })
        except httpx.HTTPError as e:
            raise PowerBIPushError(f"không kết nối được Microsoft Entra ID: {type(e).__name__}") from None
        if r.status_code != 200:
            try:
                body = r.json()
                detail = f"{body.get('error', '')}: {str(body.get('error_description', '')).splitlines()[0][:200]}"
            except (ValueError, AttributeError, IndexError):
                detail = f"HTTP {r.status_code}"
            raise PowerBIPushError(f"lấy token Microsoft Entra ID thất bại ({detail}). Kiểm tra POWERBI_TENANT_ID, "
                                   "POWERBI_CLIENT_ID, POWERBI_CLIENT_SECRET")
        body = r.json()
        self._token = body["access_token"]
        self._token_until = self._clock() + max(60, int(body.get("expires_in", 3600)) - 300)
        return self._token

    # ------------------------------------------------------------------ request
    def request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> httpx.Response:
        refreshed = False
        for attempt in range(5):
            headers = {"Authorization": f"Bearer {self.token()}"}
            try:
                r = self.http.request(method, self.base + path, json=json_body, params=params, headers=headers)
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise PowerBIPushError(f"không kết nối được Power BI: {type(e).__name__}") from None
                self._sleep(2 ** attempt)
                continue
            if r.status_code == 401 and not refreshed:           # token hết hạn sớm: lấy lại một lần
                self._token, refreshed = None, True
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** attempt
                self._sleep(min(max(wait, 1), 60))
                continue
            if r.status_code >= 400:
                raise PowerBIPushError(self._error(r))
            return r
        raise PowerBIPushError("Power BI không phản hồi sau nhiều lần thử")

    def _error(self, r: httpx.Response) -> str:
        try:
            err = r.json().get("error") or {}
            detail = " ".join(str(x) for x in (err.get("code"), err.get("message")) if x)[:300]
        except (ValueError, AttributeError):
            detail = ""
        hint = ""
        if r.status_code in (401, 403):
            hint = (" — service principal chưa có quyền: thêm app vào workspace (Member/Admin) và bật tenant setting "
                    "cho phép service principal dùng Power BI API")
        elif r.status_code == 404:
            hint = " — không thấy workspace/model/bảng, hoặc service principal chưa được thêm vào workspace"
        return f"Power BI trả HTTP {r.status_code}{': ' + detail if detail else ''}{hint}"

    # ------------------------------------------------------------------ push semantic model
    def find_model(self, name: str) -> dict[str, Any] | None:
        found = [d for d in self.request("GET", "/datasets").json().get("value", []) if d.get("name") == name]
        if not found:
            return None
        push = [d for d in found if d.get("addRowsAPIEnabled")]
        if not push:
            raise PowerBIPushError(f"workspace đã có semantic model tên '{name}' nhưng không phải push model; "
                                   "đặt powerbi.push.model tên khác")
        return push[0]

    def create_model(self, name: str, tables: list[dict[str, Any]], retention: str) -> str:
        policy = "None" if retention == "none" else retention
        r = self.request("POST", "/datasets", params={"defaultRetentionPolicy": policy},
                         json_body={"name": name, "defaultMode": "Push", "tables": tables})
        return r.json()["id"]

    def tables(self, model_id: str) -> dict[str, dict[str, str]]:
        """{bảng: {cột: kiểu}}"""
        r = self.request("GET", f"/datasets/{model_id}/tables")
        return {t["name"]: {c["name"]: c.get("dataType", "String") for c in t.get("columns", [])}
                for t in r.json().get("value", [])}

    def put_table(self, model_id: str, table: dict[str, Any]) -> None:
        self.request("PUT", f"/datasets/{model_id}/tables/{quote(table['name'], safe='')}", json_body=table)

    def delete_rows(self, model_id: str, table: str) -> None:
        self.request("DELETE", f"/datasets/{model_id}/tables/{quote(table, safe='')}/rows")

    def post_rows(self, model_id: str, table: str, rows: list[dict[str, Any]]) -> None:
        # tối đa 120 request POST rows mỗi phút mỗi model: tự chờ khi chạm ngưỡng
        while self._posts and self._clock() - self._posts[0] >= 60:
            self._posts.popleft()
        if len(self._posts) >= POSTS_PER_MINUTE:
            self._sleep(60 - (self._clock() - self._posts[0]))
            self._posts.popleft()
        self._posts.append(self._clock())
        self.request("POST", f"/datasets/{model_id}/tables/{quote(table, safe='')}/rows", json_body={"rows": rows})


# ----------------------------------------------------------------------------- pusher
class PowerBIPusher:
    """Đẩy các dataset Power BI lên Power BI Service theo lịch (powerbi.push trong dashboards.yaml)."""

    def __init__(self, engine: DashboardEngine, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time):
        self.engine = engine
        self.catalog: DashboardCatalog = engine.catalog
        cfg = self.catalog.config.powerbi.push
        if cfg is None:
            raise DashboardError("dashboards.yaml chưa có powerbi.push")
        self.cfg = cfg
        self.interval = parse_every(cfg.every, "powerbi.push.every")
        self.model = model_name(cfg, engine.ws.name)
        self.state_path = state_path(engine.ws.path)
        self._transport, self._sleep, self._now = transport, sleep, now
        self._lock = threading.Lock()
        state = read_state(engine.ws.path)
        self.last_run: datetime | None = (datetime.fromisoformat(state["pushed_at"])
                                          if state.get("pushed_at") else None)
        self.last_result: dict[str, str] = state.get("datasets") or {}
        self._history: list[list[float]] = state.get("history") or []      # [thời điểm, số dòng] trong 1 giờ qua

    def client(self) -> PowerBIClient:
        """Client dùng chung giữa các lần đẩy (giữ token tới gần hết hạn, giữ bộ đếm 120 request/phút)."""
        if getattr(self, "_client", None) is None:
            self._client = PowerBIClient(self.cfg, transport=self._transport, sleep=self._sleep)
        return self._client

    def close(self) -> None:
        if getattr(self, "_client", None) is not None:
            self._client.close()
            self._client = None

    def check(self) -> str:
        """Kiểm tra token + quyền vào workspace (cho `bizagent check`)."""
        found = self.client().find_model(self.model)
        return (f"model '{self.model}' đã có" if found else f"chưa có model '{self.model}' (tạo ở lần đẩy đầu)")

    def rows_last_hour(self) -> int:
        cutoff = self._now() - 3600
        self._history = [h for h in self._history if h[0] > cutoff]
        return int(sum(h[1] for h in self._history))

    # ------------------------------------------------------------------ đẩy
    def push_all(self) -> dict[str, str]:
        with self._lock:                       # lịch nền và nút "Đẩy ngay" không chạy chồng nhau
            return self._push_all()

    def _push_all(self) -> dict[str, str]:
        out: dict[str, str] = {}
        data: dict[str, QueryResult] = {}
        limit = min(self.catalog.config.powerbi.max_rows, MAX_TABLE_ROWS[self.cfg.retention])
        query = {"period": self.cfg.period} if self.cfg.period else {}
        for name in self.catalog.powerbi_push_datasets():
            try:
                r = self.engine.run_dataset(name, dataset_params(self.engine, name, query), SYSTEM_ROLE,
                                            limit=limit, use_cache=False)
                table_schema(name, r)          # kiểm tra số cột / tên cột sớm
                data[name] = r
            except Exception as e:
                log.error("Đẩy Power BI %s: chạy dataset lỗi: %s", name, e)
                out[name] = f"lỗi: {e}"
        if not data:
            return self._finish(out, None)
        model_id = None
        try:
            client = self.client()
        except PowerBIPushError as e:
            return self._finish({**out, **{n: f"lỗi: {e}" for n in data}}, None)
        try:
            found = client.find_model(self.model)
            if found is None:
                if out:
                    msg = (f"chưa tạo model '{self.model}' vì dataset {', '.join(out)} lỗi "
                           "(Power BI không cho thêm bảng vào push model đã tạo)")
                    return self._finish({**out, **{n: f"chưa đẩy: {msg}" for n in data}}, None)
                tables = [table_schema(n, r) for n, r in data.items()]
                model_id = client.create_model(self.model, tables, self.cfg.retention)
                log.info("Đã tạo push semantic model '%s' (%s) trên Power BI", self.model, model_id)
                remote = {t["name"]: {c["name"]: c["dataType"] for c in t["columns"]} for t in tables}
                fresh = True
            else:
                fresh = False
                model_id = found["id"]
                remote = client.tables(model_id)
        except PowerBIPushError as e:
            return self._finish({**out, **{n: f"lỗi: {e}" for n in data}}, None)
        for name, r in data.items():
            try:
                out[name] = self._push_table(client, model_id, name, r, remote, fresh)
            except Exception as e:
                log.error("Đẩy Power BI %s lỗi: %s", name, e)
                out[name] = f"lỗi: {e}"
        return self._finish(out, model_id)

    def _push_table(self, client: PowerBIClient, model_id: str, name: str, r: QueryResult,
                    remote: dict[str, dict[str, str]], fresh: bool = False) -> str:
        if name not in remote:
            raise PowerBIPushError(
                f"model '{self.model}' chưa có bảng {name}: Power BI không cho thêm bảng vào push model đã tạo. "
                "Đặt powerbi.push.model tên mới (bizagent tạo model mới) hoặc xoá model cũ trên Power BI")
        table = table_schema(name, r, remote[name])
        if not _same_schema(table, remote[name]):
            client.put_table(model_id, table)
            log.info("Power BI: cập nhật cột bảng %s", name)
        rows = to_rows(r, table)
        used = self.rows_last_hour()
        if used + len(rows) > ROWS_PER_HOUR:
            raise PowerBIPushError(f"vượt giới hạn {ROWS_PER_HOUR:,} dòng/giờ của Power BI (giờ qua đã đẩy {used:,}, "
                                   f"lần này {len(rows):,}); giãn powerbi.push.every hoặc giảm dữ liệu")
        if self.cfg.mode == "replace" and not fresh:          # model vừa tạo thì chưa có dòng nào
            client.delete_rows(model_id, name)
        sent = 0
        for batch in batches(rows, self.cfg.batch_rows):
            client.post_rows(model_id, name, batch)
            sent += len(batch)
            self._history.append([self._now(), len(batch)])
        cut = ", BỊ CẮT" if r.truncated else ""
        return f"ok ({sent} dòng, {'thay toàn bộ' if self.cfg.mode == 'replace' else 'thêm vào'}{cut})"

    def _finish(self, out: dict[str, str], model_id: str | None) -> dict[str, str]:
        self.last_run = datetime.now(UTC)
        self.last_result = out
        self.rows_last_hour()
        state = {"pushed_at": self.last_run.isoformat(), "model": self.model, "model_id": model_id,
                 "workspace_id": self.cfg.workspace_id, "mode": self.cfg.mode, "datasets": out,
                 "history": self._history}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as e:
            log.error("Không ghi được %s: %s", self.state_path, e)
        return out

    async def run_forever(self) -> None:
        log.info("Đẩy dữ liệu lên Power BI (model '%s') mỗi %s", self.model, self.cfg.every)
        while True:
            if self.last_run is None or datetime.now(UTC) - self.last_run >= self.interval:
                try:
                    await asyncio.to_thread(self.push_all)
                except Exception:
                    log.exception("Đẩy Power BI lỗi")
            await asyncio.sleep(30)


def state_path(ws_path: Path) -> Path:
    return ws_path / "data" / "powerbi" / STATE_FILE


def read_state(ws_path: Path) -> dict[str, Any]:
    """Trạng thái lần đẩy cuối (ghi cả khi chạy bằng lệnh `bizagent powerbi-push`)."""
    try:
        return json.loads(state_path(ws_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
