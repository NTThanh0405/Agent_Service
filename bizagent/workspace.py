"""Nạp một workspace (cấu hình của MỘT doanh nghiệp).

Cấu trúc thư mục workspace:

    workspaces/<ten>/
      workspace.yaml   thông tin doanh nghiệp, ngôn ngữ, múi giờ, role -> nguồn được xem
      sources.yaml     các nguồn dữ liệu (database, API)
      agents.yaml      model AI và giới hạn của agent
      users.yaml       (không commit) người dùng các kênh chat và role của họ
      prompts/         ghi đè prompt lõi (Jinja2)
      knowledge/       tài liệu nghiệp vụ (*.md) được ghép vào system prompt
      .env             (không commit) bí mật riêng của workspace

Giá trị dạng ${TEN_BIEN} hoặc ${TEN_BIEN:-mac_dinh} được thay bằng biến môi trường lúc dùng,
nên file YAML không bao giờ chứa mật khẩu. ${WORKSPACE_DIR} là đường dẫn tuyệt đối tới workspace.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_WORKSPACES_DIR = "workspaces"
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class WorkspaceError(Exception):
    """Cấu hình workspace sai hoặc thiếu."""


# ----------------------------------------------------------------------------- thay biến môi trường
def expand_env(value: Any, extra: dict[str, str] | None = None) -> Any:
    """Thay ${VAR} / ${VAR:-default} trong chuỗi (đệ quy trong dict/list). Thiếu biến mà không có mặc định -> lỗi."""
    if isinstance(value, dict):
        return {k: expand_env(v, extra) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, extra) for v in value]
    if not isinstance(value, str):
        return value
    lookup = {**os.environ, **(extra or {})}

    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        if name in lookup and lookup[name] != "":
            return lookup[name]
        if default is not None:
            return default
        raise WorkspaceError(f"Thiếu biến môi trường {name} (đặt trong .env hoặc .env của workspace)")

    return _ENV_PATTERN.sub(repl, value)


# ----------------------------------------------------------------------------- mô hình cấu hình
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StateConfig(_Strict):
    """Nơi lưu phiên chat (checkpoint LangGraph), danh sách cuộc trò chuyện, audit log, token kênh chat."""

    url: str = "sqlite:///${WORKSPACE_DIR}/data/state.db"   # hoặc postgresql+psycopg://user:pass@host/db
    idle_days: int = 30                 # cuộc trò chuyện không dùng quá số ngày này thì bị xoá
    max_threads_per_user: int = 20      # vượt thì xoá cuộc cũ nhất
    audit_days: int = 180               # audit log giữ bao lâu


class WorkspaceInfo(_Strict):
    name: str
    display_name: str = ""
    description: str = ""
    language: str = "vi"
    timezone: str = "Asia/Ho_Chi_Minh"
    currency: str = "VND"
    week_start: str = "monday"
    # role -> danh sách nguồn được xem ("*" = mọi nguồn). Role không có ở đây thì không xem được gì.
    roles: dict[str, list[str]] = Field(default_factory=lambda: {"admin": ["*"]})
    # Biến tuỳ ý cho prompt: {{ vars.ten_bien }} trong template.
    prompt_vars: dict[str, Any] = Field(default_factory=dict)
    state: StateConfig = Field(default_factory=StateConfig)
    # Role được thêm/xoá người dùng (qua lệnh chat hoặc trang /admin) và cấp mọi nguồn
    admin_roles: list[str] = Field(default_factory=lambda: ["admin"])
    # Role được cấp/thu hồi quyền xem nguồn (chỉ nguồn chính mình xem được) cho người khác, có hạn dùng
    grant_roles: list[str] = Field(default_factory=lambda: ["admin"])

    @field_validator("week_start")
    @classmethod
    def _week(cls, v: str) -> str:
        if v not in ("monday", "sunday"):
            raise ValueError("week_start phải là monday hoặc sunday")
        return v


class SourceConfig(BaseModel):
    """Một nguồn dữ liệu. Các khoá ngoài danh sách dưới đây được giữ trong `options` cho plugin dùng."""

    model_config = ConfigDict(extra="allow")
    type: str                                   # sql | rest | <plugin>
    description: str = ""
    enabled: bool = True


class AgentSettings(_Strict):
    model: str = "default"                      # tên alias trong `models` hoặc "provider:model"
    max_steps: int = 12                         # số vòng gọi tool tối đa mỗi câu hỏi
    timeout_seconds: int = 120
    max_tokens: int = 3000
    temperature: float | None = None            # để null: một số model (vd Claude đời mới) từ chối temperature


class ModelSpec(_Strict):
    """Khai báo đầy đủ một model (khi "provider:model" chưa đủ, vd API tương thích OpenAI)."""

    provider: str
    model: str
    base_url: str | None = None                 # vd http://localhost:11434 hoặc https://openrouter.ai/api/v1
    api_key_env: str | None = None              # tên biến môi trường chứa API key (không ghi key vào đây)
    params: dict[str, Any] = Field(default_factory=dict)


class ReviewerSettings(_Strict):
    enabled: bool = False       # đối chiếu con số trong câu trả lời với dữ liệu tool trả về (bằng code, không tốn tiền)
    add_note: bool = True       # còn số chưa đối chiếu được thì thêm dòng lưu ý cuối câu trả lời
    min_value: float = 10       # bỏ qua số nhỏ (<= giá trị này): số thứ tự, số ngày...


class LimitSettings(_Strict):
    """Giới hạn dùng: chống quá tải và chống tốn tiền API ngoài ý muốn."""

    max_concurrent: int = 8                 # số câu hỏi xử lý cùng lúc trong tiến trình (câu sau chờ tới lượt)
    per_user_per_minute: int = 10           # số câu hỏi tối đa mỗi người mỗi phút (0 = không giới hạn)
    daily_tokens_per_user: int = 0          # tổng token (vào + ra) mỗi người mỗi ngày (0 = không giới hạn)
    exempt_roles: list[str] = Field(default_factory=list)   # role không bị giới hạn tần suất / token

    @field_validator("max_concurrent")
    @classmethod
    def _conc(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_concurrent phải >= 1")
        return v


class TeamMemberSettings(_Strict):
    """Một agent chuyên trách trong chế độ nhiều agent. Để trống (null) thì lấy theo mục `agent`."""

    enabled: bool = True
    model: str | None = None                    # alias trong `models`, "default" hoặc "provider:model"
    max_steps: int | None = None                # số vòng gọi tool tối đa (chỉ analyst, designer gọi tool)
    max_tokens: int | None = None
    temperature: float | None = None

    @field_validator("max_steps")
    @classmethod
    def _steps(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_steps phải >= 1")
        return v


class MultiAgentSettings(_Strict):
    """Chế độ nhiều agent: router chọn đường -> analyst lấy số liệu -> advisor đánh giá, đề xuất;
    designer lo dashboard. Tắt (mặc định) thì dùng một agent như cũ."""

    enabled: bool = False
    router: TeamMemberSettings = Field(default_factory=TeamMemberSettings)    # tắt: mọi câu đi analyst
    analyst: TeamMemberSettings = Field(default_factory=TeamMemberSettings)   # luôn bật
    advisor: TeamMemberSettings = Field(default_factory=TeamMemberSettings)   # tắt: không có phần đề xuất
    designer: TeamMemberSettings = Field(default_factory=TeamMemberSettings)  # tắt: analyst tự làm dashboard

    @field_validator("analyst")
    @classmethod
    def _analyst(cls, v: TeamMemberSettings) -> TeamMemberSettings:
        if not v.enabled:
            raise ValueError("analyst là agent truy vấn dữ liệu, không tắt được")
        return v


AGENT_NAMES = ("router", "analyst", "advisor", "designer")


class AgentsConfig(_Strict):
    default_model: str = "anthropic:claude-sonnet-5-5"
    models: dict[str, str | ModelSpec] = Field(default_factory=dict)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    multi_agent: MultiAgentSettings = Field(default_factory=MultiAgentSettings)
    reviewer: ReviewerSettings = Field(default_factory=ReviewerSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)

    def settings_for(self, name: str) -> AgentSettings:
        """Cấu hình của một agent chuyên trách: giá trị riêng trong multi_agent.<name>, thiếu thì lấy từ `agent`."""
        if name not in AGENT_NAMES:
            raise ValueError(f"Không có agent '{name}'. Có: {', '.join(AGENT_NAMES)}")
        member: TeamMemberSettings = getattr(self.multi_agent, name)
        override = {k: v for k, v in member.model_dump(exclude={"enabled"}).items() if v is not None}
        return self.agent.model_copy(update=override)


class UserEntry(_Strict):
    id: str                                     # "telegram:123456789", "web:ten", ...
    name: str = ""
    role: str
    password_hash: str | None = None            # tài khoản web: tạo bằng `bizagent hash-password`


@dataclass
class Workspace:
    path: Path
    info: WorkspaceInfo
    sources: dict[str, SourceConfig]
    agents: AgentsConfig
    users: list[UserEntry] = field(default_factory=list)
    # Gắn bởi StateStore.attach(): người dùng thêm qua lệnh/trang quản trị, và quyền cấp riêng có hạn dùng
    user_provider: Callable[[], list[UserEntry]] | None = None
    grant_provider: Callable[[str], list[str]] | None = None

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def prompts_dir(self) -> Path:
        return self.path / "prompts"

    @property
    def knowledge_dir(self) -> Path:
        return self.path / "knowledge"

    def env(self) -> dict[str, str]:
        """Biến đặc biệt dùng được trong YAML."""
        return {"WORKSPACE_DIR": self.path.as_posix()}

    def local_time(self, dt: datetime | None) -> datetime | None:
        """Giờ lưu trong DB (UTC, naive hoặc có tzinfo) -> giờ theo múi giờ của workspace để hiển thị."""
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ZoneInfo(self.info.timezone))

    def all_users(self) -> list[UserEntry]:
        """users.yaml (ưu tiên) + người dùng lưu trong state DB."""
        out = list(self.users)
        if self.user_provider:
            ids = {u.id for u in out}
            out += [u for u in self.user_provider() if u.id not in ids and u.role in self.info.roles]
        return out

    def role_of(self, user_id: str) -> str | None:
        for u in self.all_users():
            if u.id == user_id:
                return u.role
        return None

    def sources_for(self, user_id: str | None, role: str | None) -> list[str]:
        """Nguồn theo role + nguồn được cấp riêng cho người này (còn hạn)."""
        base = self.sources_for_role(role)
        if role is None or not user_id or not self.grant_provider:
            return base
        extra = set(self.grant_provider(user_id))
        enabled = [n for n, s in self.sources.items() if s.enabled]
        return base + [n for n in enabled if n in extra and n not in base]

    def sources_for_role(self, role: str | None) -> list[str]:
        """Tên các nguồn (đang bật) mà role được xem."""
        if role is None:
            return []
        allowed = self.info.roles.get(role, [])
        enabled = [n for n, s in self.sources.items() if s.enabled]
        if "*" in allowed:
            return enabled
        return [n for n in enabled if n in allowed]


# ----------------------------------------------------------------------------- nạp từ đĩa
def _read_yaml(path: Path, required: bool) -> dict:
    if not path.exists():
        if required:
            raise WorkspaceError(f"Thiếu file {path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise WorkspaceError(f"{path}: YAML sai cú pháp: {e}") from e
    if not isinstance(data, dict):
        raise WorkspaceError(f"{path}: nội dung phải là một mapping")
    return data


def resolve_workspace_path(name_or_path: str | None = None, workspaces_dir: str | Path | None = None) -> Path:
    """Tìm thư mục workspace: đường dẫn trực tiếp, hoặc tên trong thư mục workspaces/."""
    name_or_path = name_or_path or os.environ.get("BIZAGENT_WORKSPACE") or "demo"
    p = Path(name_or_path)
    if (p / "workspace.yaml").exists():
        return p.resolve()
    root = Path(workspaces_dir or os.environ.get("BIZAGENT_WORKSPACES_DIR") or DEFAULT_WORKSPACES_DIR)
    candidate = root / name_or_path
    if (candidate / "workspace.yaml").exists():
        return candidate.resolve()
    raise WorkspaceError(f"Không tìm thấy workspace '{name_or_path}' (đã tìm {p} và {candidate})")


def load_env_files(ws_path: Path) -> None:
    """Nạp .env của workspace (không ghi đè biến đã có). .env gốc của dự án được CLI nạp trước."""
    from dotenv import load_dotenv

    env_file = ws_path / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def load_workspace(name_or_path: str | None = None, workspaces_dir: str | Path | None = None) -> Workspace:
    path = resolve_workspace_path(name_or_path, workspaces_dir)
    load_env_files(path)
    try:
        raw_info = _read_yaml(path / "workspace.yaml", required=True)
        raw_info.setdefault("name", path.name)
        info = WorkspaceInfo(**raw_info)

        raw_sources = _read_yaml(path / "sources.yaml", required=False).get("sources") or {}
        sources = {name: SourceConfig(**cfg) for name, cfg in raw_sources.items()}

        agents = AgentsConfig(**_read_yaml(path / "agents.yaml", required=False))

        raw_users = _read_yaml(path / "users.yaml", required=False).get("users") or []
        users = [UserEntry(**{**u, "id": str(u.get("id"))}) for u in raw_users]
    except WorkspaceError:
        raise
    except Exception as e:  # lỗi kiểu dữ liệu của pydantic -> thông báo dễ đọc
        raise WorkspaceError(f"Workspace {path.name}: cấu hình không hợp lệ: {e}") from e

    for role, names in info.roles.items():
        unknown = [n for n in names if n != "*" and n not in sources]
        if unknown:
            raise WorkspaceError(f"workspace.yaml: role '{role}' trỏ tới nguồn không tồn tại: {unknown}")
    for u in users:
        if u.role not in info.roles:
            raise WorkspaceError(f"users.yaml: người dùng {u.id} có role '{u.role}' chưa khai báo trong roles")

    return Workspace(path=path, info=info, sources=sources, agents=agents, users=users)
