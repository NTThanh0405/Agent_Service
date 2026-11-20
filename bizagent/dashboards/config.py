"""Cấu hình dashboard của workspace (dashboard-as-code).

    workspaces/<ten>/
      dashboards.yaml          bật/tắt: live (dashboard sống) và powerbi (đưa dữ liệu sang Power BI)
      datasets.yaml            các dataset dùng chung (một truy vấn có tham số trên một nguồn)
      dashboards/<slug>.yaml   mỗi file một dashboard: bộ lọc + widget (+ dataset riêng)
      dashboards/<slug>.js     (tuỳ chọn) code JS cho widget type: custom

Trong SQL của dataset, tham số viết {{ten}}; giá trị được chèn dưới dạng literal SQL đã thoát ký tự,
rồi cả câu đi qua guard như mọi câu SQL khác. Bộ lọc kiểu period sinh sẵn {{start}} {{end}} {{prev_start}} {{prev_end}}.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bizagent.workspace import Workspace, WorkspaceError, expand_env

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PARAM = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
WIDGET_TYPES = ("kpi", "line", "bar", "area", "pie", "table", "custom")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParamSpec(_Strict):
    type: Literal["string", "number", "date"] = "string"
    default: Any = None


class DatasetSpec(_Strict):
    source: str
    sql: str | None = None
    operation: str | None = None                    # nguồn REST
    params: dict[str, Any] = Field(default_factory=dict)   # tham số cố định cho operation
    description: str = ""
    max_rows: int = 5000                            # giới hạn dòng cho dashboard
    inputs: dict[str, ParamSpec] = Field(default_factory=dict)   # tham số {{x}} ngoài bộ lọc period

    @model_validator(mode="after")
    def _one(self) -> DatasetSpec:
        if (self.sql is None) == (self.operation is None):
            raise ValueError("dataset cần đúng một trong sql / operation")
        return self

    def placeholders(self) -> set[str]:
        return set(PARAM.findall(self.sql or ""))


class FilterSpec(_Strict):
    type: Literal["period", "select", "text"] = "period"
    label: str = ""
    default: Any = None
    # select: lấy lựa chọn từ một dataset (cột `column`), hoặc danh sách `options` cố định
    dataset: str | None = None
    column: str | None = None
    options: list[Any] = Field(default_factory=list)
    allow_all: bool = True                          # select: có lựa chọn "Tất cả" (giá trị NULL)


class WidgetSpec(_Strict):
    type: str
    title: str = ""
    dataset: str | None = None
    x: str | None = None                            # line/bar/area: cột trục X; pie: cột nhãn
    y: list[str] = Field(default_factory=list)      # cột giá trị; pie: cột đầu tiên
    series: str | None = None                       # cột tách thành nhiều đường/cột (pivot)
    stack: bool = False
    horizontal: bool = False
    value: str | None = None                        # kpi: cột giá trị (dòng đầu)
    compare: str | None = None                      # kpi: cột kỳ trước để tính % thay đổi
    format: Literal["number", "currency", "percent", "text"] = "number"
    columns: list[str] = Field(default_factory=list)   # table: cột hiển thị (trống = tất cả)
    width: int = 6                                  # 1..12 (lưới 12 cột)
    height: int = 320
    script: str | None = None                       # custom: tên file .js trong dashboards/
    note: str = ""

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in WIDGET_TYPES:
            raise ValueError(f"widget type phải là một trong {WIDGET_TYPES}")
        return v

    @field_validator("width")
    @classmethod
    def _width(cls, v: int) -> int:
        return max(1, min(12, v))

    @model_validator(mode="after")
    def _needs(self) -> WidgetSpec:
        if self.type != "custom" and not self.dataset:
            raise ValueError(f"widget {self.type} cần dataset")
        if self.type in ("line", "bar", "area") and (not self.x or not self.y):
            raise ValueError(f"widget {self.type} cần x và y")
        if self.type == "pie" and (not self.x or not self.y):
            raise ValueError("widget pie cần x (nhãn) và y (giá trị)")
        if self.type == "kpi" and not self.value:
            raise ValueError("widget kpi cần value")
        if self.type == "custom" and not self.script:
            raise ValueError("widget custom cần script")
        return self


class DashboardSpec(_Strict):
    title: str
    description: str = ""
    refresh_seconds: int = 60
    roles: list[str] = Field(default_factory=list)  # trống = mọi role xem được nguồn của dashboard
    filters: dict[str, FilterSpec] = Field(default_factory=dict)
    datasets: dict[str, DatasetSpec] = Field(default_factory=dict)
    widgets: list[WidgetSpec]
    created_by: str | None = None                   # "agent" khi do agent tạo

    @field_validator("refresh_seconds")
    @classmethod
    def _refresh(cls, v: int) -> int:
        return max(10, v)


class PowerBIReport(_Strict):
    title: str
    url: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)


class PowerBIExport(_Strict):
    every: str = "1h"                               # 30m, 1h, 1d
    formats: list[Literal["parquet", "csv"]] = Field(default_factory=lambda: ["parquet", "csv"])
    dir: str = "data/powerbi"


GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class PowerBIPush(_Strict):
    """Tự đẩy dữ liệu lên Power BI Service (push semantic model qua REST API). Xem docs/DASHBOARDS.md."""

    every: str = "1h"                               # 30m, 1h, 1d
    workspace_id: str                               # ID workspace Power BI (GUID trong URL .../groups/<id>/...)
    model: str = ""                                 # tên semantic model trên Power BI; trống = "bizagent <workspace>"
    datasets: list[str] = Field(default_factory=list)   # trống = như powerbi.datasets; "*" = tất cả
    mode: Literal["replace", "append"] = "replace"  # replace: xoá dòng cũ rồi đẩy lại; append: chỉ thêm dòng
    period: str | None = None                       # kỳ cho {{start}}/{{end}}; trống = powerbi.default_period
    retention: Literal["none", "basicFIFO"] = "none"    # basicFIFO: Power BI giữ 200.000 dòng mới nhất mỗi bảng
    batch_rows: int = 10_000                        # số dòng mỗi request (Power BI cho tối đa 10.000)
    timeout_seconds: float = 60
    authority: str = "https://login.microsoftonline.com"   # đổi cho cloud riêng (Gov/China)
    api_url: str = "https://api.powerbi.com"

    @field_validator("workspace_id")
    @classmethod
    def _guid(cls, v: str) -> str:
        v = v.strip()
        if not GUID.match(v):
            raise ValueError("powerbi.push.workspace_id phải là GUID của workspace Power BI (không dùng My Workspace)")
        return v.lower()

    @field_validator("batch_rows")
    @classmethod
    def _batch(cls, v: int) -> int:
        return max(1, min(10_000, v))


class PowerBIConfig(_Strict):
    enabled: bool = False
    datasets: list[str] = Field(default_factory=list)   # dataset đưa sang Power BI ("*" = tất cả)
    max_rows: int = 1_000_000
    default_period: str = "365 ngày qua"            # cho dataset có {{start}}/{{end}}; đổi bằng ?period=... trên URL
    export: PowerBIExport | None = None
    push: PowerBIPush | None = None
    reports: dict[str, PowerBIReport] = Field(default_factory=dict)  # báo cáo Power BI có sẵn để agent gửi link


class LiveConfig(_Strict):
    enabled: bool = True
    agent_can_create: list[str] = Field(default_factory=lambda: ["admin"])   # role được nhờ agent tạo dashboard
    cache_seconds: int = 30                         # nhiều người xem cùng lúc vẫn chỉ truy vấn một lần


class DashboardsConfig(_Strict):
    public_url: str = ""                            # địa chỉ người dùng mở, vd https://bi.congty.vn
    live: LiveConfig = Field(default_factory=LiveConfig)
    powerbi: PowerBIConfig = Field(default_factory=PowerBIConfig)


# ----------------------------------------------------------------------------- nạp
def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise WorkspaceError(f"{path.name}: YAML sai cú pháp: {e}") from e
    if not isinstance(data, dict):
        raise WorkspaceError(f"{path.name}: nội dung phải là mapping")
    return data


class DashboardCatalog:
    """Toàn bộ dashboard + dataset của một workspace. Gọi reload() để nạp lại sau khi sửa file."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self._stamp: tuple = ()
        self.reload()

    @property
    def dir(self) -> Path:
        return self.ws.path / "dashboards"

    def _files_stamp(self) -> tuple:
        files = [self.ws.path / "dashboards.yaml", self.ws.path / "datasets.yaml"]
        if self.dir.is_dir():
            files += sorted(self.dir.glob("*.yaml"))
        return tuple((str(f), f.stat().st_mtime_ns) for f in files if f.exists())

    def maybe_reload(self) -> None:
        """Nạp lại khi file dashboard đổi: sửa YAML là thấy ngay, không cần restart. File lỗi thì giữ bản cũ."""
        if self._files_stamp() != self._stamp:
            try:
                self.reload()
            except WorkspaceError as e:
                self.errors["_config"] = str(e)

    def reload(self) -> None:
        self._stamp = self._files_stamp()
        cfg_path = self.ws.path / "dashboards.yaml"
        try:
            raw = expand_env(_yaml(cfg_path), self.ws.env()) if cfg_path.exists() else {"live": {"enabled": False}}
            self.config = DashboardsConfig(**raw)
            ds_path = self.ws.path / "datasets.yaml"
            raw_ds = (_yaml(ds_path).get("datasets") or {}) if ds_path.exists() else {}
            self.datasets = {k: DatasetSpec(**v) for k, v in raw_ds.items()}
        except WorkspaceError:
            raise
        except Exception as e:
            raise WorkspaceError(f"dashboards.yaml / datasets.yaml không hợp lệ: {e}") from e
        self.dashboards: dict[str, DashboardSpec] = {}
        self.errors: dict[str, str] = {}
        if self.dir.is_dir():
            for f in sorted(self.dir.glob("*.yaml")):
                try:
                    self.dashboards[f.stem] = self.parse_dashboard(f.stem, _yaml(f))
                except Exception as e:                    # một dashboard lỗi không làm hỏng dashboard khác
                    self.errors[f.stem] = str(e)
        for name, ds in self.datasets.items():
            self._check_dataset(name, ds)
        for name in self.config.powerbi.datasets:
            if name != "*" and name not in self.datasets:
                raise WorkspaceError(f"dashboards.yaml: powerbi.datasets có '{name}' không có trong datasets.yaml")
        push = self.config.powerbi.push
        if push is not None:
            for name in push.datasets:
                if name != "*" and name not in self.datasets:
                    raise WorkspaceError(f"dashboards.yaml: powerbi.push.datasets có '{name}' không có trong "
                                         "datasets.yaml")
            if len(self.powerbi_push_datasets()) > 75:
                raise WorkspaceError("powerbi.push: Power BI cho tối đa 75 bảng trong một semantic model")

    def _check_dataset(self, name: str, ds: DatasetSpec) -> None:
        if ds.source not in self.ws.sources:
            raise WorkspaceError(f"dataset {name}: nguồn '{ds.source}' không tồn tại")

    def parse_dashboard(self, slug: str, raw: dict) -> DashboardSpec:
        if not SLUG.match(slug):
            raise WorkspaceError(f"Tên dashboard '{slug}' chỉ được dùng a-z, 0-9, _ và -")
        spec = DashboardSpec(**raw)
        for name, ds in spec.datasets.items():
            self._check_dataset(name, ds)
        for w in spec.widgets:
            if w.dataset and w.dataset not in spec.datasets and w.dataset not in self.datasets:
                raise WorkspaceError(f"widget '{w.title or w.type}' dùng dataset không tồn tại: {w.dataset}")
            if w.script and not (self.dir / w.script).is_file():
                raise WorkspaceError(f"widget custom: không thấy file {w.script}")
            if w.script and not w.script.endswith(".js"):
                raise WorkspaceError("script phải là file .js trong thư mục dashboards/")
        for fname, f in spec.filters.items():
            if f.type == "select" and f.dataset and f.dataset not in spec.datasets and f.dataset not in self.datasets:
                raise WorkspaceError(f"bộ lọc {fname}: dataset không tồn tại {f.dataset}")
        return spec

    def dataset(self, name: str, dashboard: DashboardSpec | None = None) -> DatasetSpec:
        if dashboard and name in dashboard.datasets:
            return dashboard.datasets[name]
        if name in self.datasets:
            return self.datasets[name]
        raise WorkspaceError(f"Không có dataset '{name}'")

    def powerbi_datasets(self) -> list[str]:
        names = self.config.powerbi.datasets
        return list(self.datasets) if "*" in names else list(names)

    def powerbi_push_datasets(self) -> list[str]:
        push = self.config.powerbi.push
        if push is None:
            return []
        if not push.datasets:
            return self.powerbi_datasets()
        return list(self.datasets) if "*" in push.datasets else list(dict.fromkeys(push.datasets))

    def sources_of(self, spec: DashboardSpec) -> set[str]:
        names = {w.dataset for w in spec.widgets if w.dataset}
        names |= {f.dataset for f in spec.filters.values() if f.dataset}
        return {self.dataset(n, spec).source for n in names}

    def can_view(self, slug: str, role: str | None, user_id: str | None = None) -> bool:
        spec = self.dashboards.get(slug)
        if spec is None or role is None:
            return False
        if spec.roles and role not in spec.roles:
            return False
        return self.sources_of(spec) <= set(self.ws.sources_for(user_id, role))

    def visible(self, role: str | None, user_id: str | None = None) -> list[str]:
        return [s for s in self.dashboards if self.can_view(s, role, user_id)]

    def link(self, slug: str) -> str:
        base = (self.config.public_url or "").rstrip("/")
        return f"{base}/d/{slug}"
