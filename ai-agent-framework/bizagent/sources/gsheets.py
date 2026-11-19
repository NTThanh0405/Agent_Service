"""Nguồn Google Sheets (chỉ đọc). Agent hỏi bằng SQL như một database (dialect duckdb).

    ke_hoach:
      type: google_sheets
      spreadsheet_id: "${KE_HOACH_SHEET_ID}"        # phần giữa /d/ và /edit trong link Google Sheets
      auth:
        type: service_account                        # khuyên dùng: share sheet cho email service account (quyền Xem)
        credentials_file: "${GOOGLE_APPLICATION_CREDENTIALS}"
        # type: api_key, key_env: GOOGLE_SHEETS_API_KEY   (chỉ với sheet công khai)
      tables:                                        # tuỳ chọn; bỏ trống = mỗi tab là một bảng
        muc_tieu: {range: "Mục tiêu!A1:H"}           # dòng đầu của range là tiêu đề cột
      cache_seconds: 300
      mask_columns: ["so_dien_thoai"]

Cài: pip install "bizagent[gsheets]" (google-auth) khi dùng service account.
Tên tab và tiêu đề cột được chuẩn hoá: "Doanh thu (VNĐ)" -> doanh_thu_vnd (describe_table cho biết tên gốc).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from bizagent.sources.base import SourceError
from bizagent.sources.tabular import TabularSource
from bizagent.tabular import slugify

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class GoogleSheetsSource(TabularSource):
    def __init__(self, name: str, config: dict[str, Any], *, transport: httpx.BaseTransport | None = None):
        super().__init__(name, config)
        self.spreadsheet_id = str(config.get("spreadsheet_id") or "")
        if not self.spreadsheet_id:
            raise SourceError(f"Nguồn {name}: thiếu spreadsheet_id")
        self.auth: dict[str, Any] = config.get("auth") or {"type": "service_account"}
        self.tables_cfg: dict[str, dict] = {slugify(k, "t"): v or {} for k, v in (config.get("tables") or {}).items()}
        self.client = httpx.Client(timeout=float(config.get("timeout_seconds", 20)), transport=transport)
        self._creds = None
        self._token_lock = threading.Lock()
        self._tabs: tuple[float, dict[str, str]] | None = None

    # ------------------------------------------------------------------ xác thực
    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        typ = self.auth.get("type", "service_account")
        if typ == "api_key":
            key = os.environ.get(self.auth.get("key_env", ""))
            if not key:
                raise SourceError(f"Nguồn {self.name}: chưa đặt biến {self.auth.get('key_env')}")
            return {}, {"key": key}
        if typ != "service_account":
            raise SourceError(f"Nguồn {self.name}: auth.type phải là service_account hoặc api_key")
        with self._token_lock:
            if self._creds is None:
                try:
                    from google.oauth2 import service_account
                except ImportError as e:
                    raise SourceError('Google Sheets cần google-auth: pip install "bizagent[gsheets]"') from e
                path = self.auth.get("credentials_file") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                if not path or not os.path.exists(path):
                    raise SourceError(f"Nguồn {self.name}: không thấy file service account ({path})")
                self._creds = service_account.Credentials.from_service_account_file(path, scopes=[SCOPE])
            if not self._creds.valid:
                from google.auth.transport.requests import Request

                self._creds.refresh(Request())
            return {"Authorization": f"Bearer {self._creds.token}"}, {}

    def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        headers, auth_params = self._auth()
        try:
            resp = self.client.get(url, params={**(params or {}), **auth_params}, headers=headers)
        except httpx.TransportError as e:
            raise SourceError(f"Nguồn {self.name}: lỗi kết nối Google Sheets ({e.__class__.__name__})") from e
        if resp.status_code == 403:
            raise SourceError(f"Nguồn {self.name}: không có quyền đọc sheet (đã share cho service account chưa?)")
        if resp.status_code >= 400:
            raise SourceError(f"Nguồn {self.name}: Google Sheets trả HTTP {resp.status_code}")
        return resp.json()

    # ------------------------------------------------------------------ bảng
    def _tab_titles(self) -> dict[str, str]:
        """{tên bảng: tên tab gốc}, cache theo cache_seconds."""
        if self._tabs and time.monotonic() - self._tabs[0] < self.cache_seconds:
            return self._tabs[1]
        data = self._get(f"{API}/{self.spreadsheet_id}", {"fields": "sheets.properties.title"})
        titles = [s["properties"]["title"] for s in data.get("sheets", [])]
        tabs = {slugify(t, "t"): t for t in titles}
        self._tabs = (time.monotonic(), tabs)
        return tabs

    def table_names(self) -> list[str]:
        return list(self.tables_cfg) if self.tables_cfg else list(self._tab_titles())

    def fetch_table(self, table: str) -> tuple[list[str], list[tuple]]:
        rng = self.tables_cfg.get(table, {}).get("range") or self._tab_titles()[table]
        if "!" not in rng and table not in self.tables_cfg:
            rng = f"'{rng}'"
        data = self._get(
            f"{API}/{self.spreadsheet_id}/values/{quote(rng, safe='')}",
            {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
        )
        values = data.get("values") or []
        if not values:
            return [], []
        header = [str(h) for h in values[0]]
        return header, [tuple(r) for r in values[1:]]

    def close(self) -> None:
        self.client.close()
