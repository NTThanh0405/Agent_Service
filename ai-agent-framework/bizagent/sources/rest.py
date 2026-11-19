"""Nguồn REST API khai báo bằng YAML (Base.vn, iPOS, MISA, CRM... mọi API trả JSON).

Agent CHỈ gọi được các `operations` khai báo sẵn, trên đúng `base_url`. Không có URL tuỳ ý.

    base_vn:
      type: rest
      base_url: "https://example.base.vn"
      description: "Đề xuất, công việc trên Base"
      auth: {type: body, param: access_token, value_env: BASE_ACCESS_TOKEN}
        # none | bearer {value_env} | header {header, value_env, prefix} | query {param, value_env}
        # | body {param, value_env} (POST form/json) | basic {user_env, password_env}
      timeout_seconds: 20
      retries: 2                      # thử lại khi 429 / 5xx / mất kết nối
      rate_limit_per_minute: 60
      cache_seconds: 60
      max_rows: 200                   # số dòng tối đa trả cho agent
      mask_columns: ["phone", "email"]
      headers: {Accept: application/json}
      operations:
        list_requests:
          description: "Danh sách đề xuất trong khoảng ngày"
          method: POST                # GET | POST (POST chỉ dùng cho API đọc dữ liệu bằng POST)
          path: "/extapi/v1/request/list"
          body_format: form           # form | json (POST)
          params: {from: "{start}", to: "{end}", page: "{page}"}
          inputs:                     # tham số agent được truyền thêm
            status: {type: string, enum: [pending, approved], description: "Trạng thái"}
          result_path: "requests"     # đường dẫn tới mảng bản ghi trong JSON
          fields: {id: id, tieu_de: name, trang_thai: status}   # tuỳ chọn: chọn + đổi tên cột
          pagination: {type: page, start: 1, max_pages: 20}
            # page {param?, start, size?, size_param?} | offset {param, size, size_param} | cursor {next_path} | none

Biến dùng trong params/path: {start} {end} (YYYY-MM-DD), {start_dmy} {end_dmy} (DD/MM/YYYY),
{start_ts} {end_ts} (unix giây), {page} {offset} {cursor} {size}, và tên các `inputs`.
Khoảng ngày lấy từ tham số `period` ("tháng này", "7 ngày qua"...) hoặc `start`/`end` do agent truyền.
Tham số có biến không có giá trị thì bị bỏ khỏi request.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from bizagent.periods import PeriodError, resolve_period
from bizagent.security.masking import mask_rows
from bizagent.sources.base import DataSource, QueryResult, SourceError, TableInfo
from bizagent.tabular import get_path, records_to_table

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_INPUT_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "date": str}
_RESERVED = {"period", "start", "end"}


class RestSource(DataSource):
    kind = "rest"

    def __init__(self, name: str, config: dict[str, Any], *, transport: httpx.BaseTransport | None = None):
        super().__init__(name, config)
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        if not self.base_url.startswith(("https://", "http://")):
            raise SourceError(f"Nguồn {name}: base_url phải bắt đầu bằng https:// (hoặc http:// trong mạng nội bộ)")
        self.operations: dict[str, dict[str, Any]] = config.get("operations") or {}
        if not self.operations:
            raise SourceError(f"Nguồn {name}: chưa khai báo operations")
        for op_name, op in self.operations.items():
            if (op.get("method") or "GET").upper() not in ("GET", "POST"):
                raise SourceError(f"{name}.{op_name}: chỉ hỗ trợ GET/POST")
            if not op.get("path"):
                raise SourceError(f"{name}.{op_name}: thiếu path")
        self.auth: dict[str, Any] = config.get("auth") or {"type": "none"}
        self.max_rows = int(config.get("max_rows", 200))
        self.retries = int(config.get("retries", 2))
        self.cache_seconds = int(config.get("cache_seconds", 60))
        self.mask_columns: list[str] = list(config.get("mask_columns") or [])
        self.max_response_bytes = int(config.get("max_response_mb", 20)) * 1024 * 1024
        self.timezone = config.get("timezone", "Asia/Ho_Chi_Minh")
        per_min = float(config.get("rate_limit_per_minute", 60))
        self._min_interval = 60.0 / per_min if per_min > 0 else 0.0
        self._last_call = 0.0
        self._rate_lock = threading.Lock()
        self._cache: dict[str, tuple[float, QueryResult]] = {}
        self.client = httpx.Client(
            timeout=float(config.get("timeout_seconds", 20)),
            headers={"Accept": "application/json", **(config.get("headers") or {})},
            follow_redirects=False,           # không cho chuyển hướng sang máy chủ khác
            transport=transport,
        )

    # ------------------------------------------------------------------ mô tả
    def health(self) -> None:
        self._auth_parts()      # kiểm tra đã có bí mật; không gọi API thật

    def list_tables(self) -> list[str]:
        return list(self.operations)

    def describe_table(self, table: str) -> TableInfo:
        op = self._op(table)
        inputs = {k: {kk: vv for kk, vv in (v or {}).items()} for k, v in (op.get("inputs") or {}).items()}
        if self._uses_period(op):
            inputs["period"] = {"type": "string", "description": "Cụm thời gian, vd 'tháng này' (hoặc start/end)"}
        desc = op.get("description", "")
        if inputs:
            desc += "\nTham số: " + json.dumps(inputs, ensure_ascii=False)
        return TableInfo(name=table, description=desc, columns=[{"name": c} for c in (op.get("fields") or {})])

    @staticmethod
    def _uses_period(op: dict[str, Any]) -> bool:
        text = json.dumps(op.get("params") or {}) + op["path"]
        return any(p in text for p in ("{start", "{end"))

    def describe_operations(self) -> list[dict[str, Any]]:
        out = []
        for name in self.operations:
            info = self.describe_table(name)
            out.append({"operation": name, "description": info.description})
        return out

    # ------------------------------------------------------------------ gọi
    def query(self, query: str, max_rows: int | None = None) -> QueryResult:
        """query = JSON {"operation": "...", "params": {...}} (để dùng chung giao diện DataSource)."""
        try:
            req = json.loads(query)
        except json.JSONDecodeError as e:
            raise SourceError('Nguồn REST nhận JSON dạng {"operation": "...", "params": {...}}') from e
        return self.call(req.get("operation", ""), req.get("params") or {}, max_rows)

    def call(self, operation: str, params: dict[str, Any] | None = None, max_rows: int | None = None,
             *, today: date | None = None) -> QueryResult:
        op = self._op(operation)
        limit = max_rows or self.max_rows
        values = self._values(op, params or {}, today)
        key = json.dumps([operation, values, limit], sort_keys=True, default=str)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]

        records: list[Any] = []
        truncated = False
        pag = op.get("pagination") or {"type": "none"}
        ptype = pag.get("type", "none")
        size = pag.get("size")
        page = int(pag.get("start", 1))
        offset = 0
        cursor: Any = None
        for _ in range(int(pag.get("max_pages", 20)) if ptype != "none" else 1):
            vals = {**values, "page": page, "offset": offset, "cursor": cursor, "size": size}
            data = self._request(operation, op, vals, ptype, pag)
            batch = get_path(data, op.get("result_path"))
            if batch is None:
                batch = []
            if isinstance(batch, dict):
                batch = [batch]
            if not isinstance(batch, list):
                raise SourceError(f"{self.name}.{operation}: result_path không trỏ tới danh sách bản ghi")
            records.extend(batch)
            if len(records) > limit:
                truncated = True
                break
            if ptype == "none" or not batch or (size and len(batch) < int(size)):
                break
            if ptype == "page":
                page += 1
            elif ptype == "offset":
                offset += len(batch)
            elif ptype == "cursor":
                cursor = get_path(data, pag.get("next_path"))
                if not cursor:
                    break
        else:
            truncated = ptype != "none"     # hết max_pages mà vẫn còn trang

        cols, rows = records_to_table(records[:limit], op.get("fields"))
        rows = mask_rows(cols, rows, self.mask_columns)
        result = QueryResult(source=self.name, columns=cols, rows=rows, truncated=truncated,
                             meta={"operation": operation, "period": [values.get("start"), values.get("end")]})
        self._cache[key] = (time.monotonic(), result)
        return result

    # ------------------------------------------------------------------ nội bộ
    def _op(self, operation: str) -> dict[str, Any]:
        if operation not in self.operations:
            raise SourceError(f"Nguồn {self.name} không có operation '{operation}'. Có: {list(self.operations)}")
        return self.operations[operation]

    def _values(self, op: dict[str, Any], params: dict[str, Any], today: date | None) -> dict[str, Any]:
        declared: dict[str, dict] = {k: (v or {}) for k, v in (op.get("inputs") or {}).items()}
        unknown = set(params) - set(declared) - _RESERVED
        if unknown:
            raise SourceError(f"Tham số không được phép: {sorted(unknown)}. Được phép: {sorted(declared)}")
        values: dict[str, Any] = {}
        for k, spec in declared.items():
            v = params.get(k, spec.get("default"))
            if v is None:
                if spec.get("required"):
                    raise SourceError(f"Thiếu tham số bắt buộc '{k}'")
                continue
            typ = _INPUT_TYPES.get(spec.get("type", "string"), str)
            if not isinstance(v, typ) or (spec.get("type") == "integer" and isinstance(v, bool)):
                raise SourceError(f"Tham số '{k}' phải có kiểu {spec.get('type', 'string')}")
            if spec.get("enum") and v not in spec["enum"]:
                raise SourceError(f"Tham số '{k}' phải là một trong {spec['enum']}")
            values[k] = v

        start = end = None
        if params.get("period"):
            from zoneinfo import ZoneInfo

            ref = today or datetime.now(ZoneInfo(self.timezone)).date()
            try:
                p = resolve_period(str(params["period"]), ref)
            except PeriodError as e:
                raise SourceError(str(e)) from e
            start, end = p.start, p.end
        if params.get("start") or params.get("end"):
            try:
                start = date.fromisoformat(str(params.get("start") or params.get("end")))
                end = date.fromisoformat(str(params.get("end") or params.get("start")))
            except ValueError as e:
                raise SourceError("start/end phải có dạng YYYY-MM-DD") from e
        if start and end:
            if end < start:
                raise SourceError("end trước start")
            max_days = int(self.config.get("max_days", 366))
            if (end - start).days + 1 > max_days:
                raise SourceError(f"Khoảng ngày dài quá {max_days} ngày")
            values.update(
                start=start.isoformat(), end=end.isoformat(),
                start_dmy=start.strftime("%d/%m/%Y"), end_dmy=end.strftime("%d/%m/%Y"),
                start_ts=int(datetime.combine(start, datetime.min.time()).timestamp()),
                end_ts=int(datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp()) - 1,
            )
        return values

    @staticmethod
    def _render(template: Any, values: dict[str, Any]) -> tuple[bool, Any]:
        """(có đủ giá trị không, giá trị). "{page}" đứng một mình giữ nguyên kiểu số."""
        if not isinstance(template, str):
            return True, template
        names = _PLACEHOLDER.findall(template)
        if any(values.get(n) is None for n in names):
            return False, None
        m = _PLACEHOLDER.fullmatch(template)
        if m:
            return True, values[m.group(1)]
        return True, _PLACEHOLDER.sub(lambda x: str(values[x.group(1)]), template)

    def _auth_parts(self) -> dict[str, Any]:
        a = self.auth
        typ = a.get("type", "none")

        def secret(env_key: str) -> str:
            env = a.get(env_key)
            val = os.environ.get(env or "")
            if not val:
                raise SourceError(f"Nguồn {self.name}: chưa đặt biến môi trường {env or '(thiếu ' + env_key + ')'}")
            return val

        if typ == "none":
            return {}
        if typ == "bearer":
            return {"headers": {"Authorization": f"Bearer {secret('value_env')}"}}
        if typ == "header":
            return {"headers": {a.get("header", "Authorization"): a.get("prefix", "") + secret("value_env")}}
        if typ == "query":
            return {"params": {a["param"]: secret("value_env")}}
        if typ == "body":
            return {"body": {a["param"]: secret("value_env")}}
        if typ == "basic":
            return {"auth": (secret("user_env"), secret("password_env"))}
        raise SourceError(f"Nguồn {self.name}: kiểu auth '{typ}' không hỗ trợ")

    def _throttle(self) -> None:
        with self._rate_lock:
            wait = self._last_call + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _request(self, operation: str, op: dict, vals: dict, ptype: str, pag: dict) -> Any:
        params: dict[str, Any] = {}
        for k, tpl in (op.get("params") or {}).items():
            ok, v = self._render(tpl, vals)
            if ok:
                params[k] = v
        # tham số phân trang tự thêm nếu khai báo tên tham số
        for p_key, v_key in (("param", {"page": "page", "offset": "offset", "cursor": "cursor"}.get(ptype)),
                             ("size_param", "size")):
            pname = pag.get(p_key)
            if pname and v_key and vals.get(v_key) is not None and pname not in params:
                params[pname] = vals[v_key]
        path_ok, path = True, op["path"]
        for n in _PLACEHOLDER.findall(op["path"]):
            if vals.get(n) is None:
                path_ok = False
            else:
                path = path.replace("{" + n + "}", quote(str(vals[n]), safe=""))
        if not path_ok:
            raise SourceError(f"{self.name}.{operation}: thiếu giá trị cho đường dẫn {op['path']}")

        auth = self._auth_parts()
        method = (op.get("method") or "GET").upper()
        kwargs: dict[str, Any] = {"headers": auth.get("headers"), "auth": auth.get("auth")}
        if method == "GET":
            kwargs["params"] = {**params, **auth.get("params", {}), **auth.get("body", {})}
        else:
            body = {**params, **auth.get("body", {})}
            kwargs["params"] = auth.get("params") or None
            if (op.get("body_format") or "json") == "form":
                kwargs["data"] = body
            else:
                kwargs["json"] = body

        url = self.base_url + path
        last_error = ""
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError as e:
                last_error = f"lỗi kết nối ({e.__class__.__name__})"
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("Retry-After", "")
                    if retry_after.isdigit() and attempt < self.retries:
                        time.sleep(min(int(retry_after), 30))
                        continue
                elif resp.status_code >= 400:
                    # không đưa URL (có thể chứa token) vào thông báo lỗi
                    raise SourceError(f"{self.name}.{operation}: HTTP {resp.status_code}")
                else:
                    if len(resp.content) > self.max_response_bytes:
                        raise SourceError(f"{self.name}.{operation}: phản hồi quá lớn")
                    try:
                        return resp.json()
                    except ValueError as e:
                        raise SourceError(f"{self.name}.{operation}: phản hồi không phải JSON") from e
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))
        raise SourceError(f"{self.name}.{operation}: thất bại sau {self.retries + 1} lần ({last_error})")

    def close(self) -> None:
        self.client.close()
