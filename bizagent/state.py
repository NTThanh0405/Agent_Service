"""Trạng thái lâu dài của một workspace: cuộc trò chuyện, audit log, khoá-giá trị (token kênh chat...).

Lưu trong SQLite (mặc định: workspaces/<ten>/data/state.db) hoặc PostgreSQL (workspace.yaml: state.url).
Nội dung hội thoại (checkpoint của LangGraph) lưu cùng nơi, xem open_checkpointer().
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import make_url

from bizagent.workspace import Workspace, expand_env

log = logging.getLogger(__name__)
meta = MetaData()

threads = Table(
    "bz_threads", meta,
    Column("user_id", String(200), primary_key=True),
    Column("thread_id", String(100), primary_key=True),
    Column("title", String(200), nullable=False, default=""),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False, index=True),
)
audit = Table(
    "bz_audit", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("user_id", String(200), nullable=False),
    Column("kind", String(50), nullable=False),
    Column("detail", Text, nullable=False, default="{}"),
)
users_t = Table(
    "bz_users", meta,
    Column("id", String(200), primary_key=True),
    Column("name", String(200), nullable=False, default=""),
    Column("role", String(50), nullable=False),
    Column("password_hash", String(300), nullable=True),
    Column("created_by", String(200), nullable=False, default=""),
    Column("created_at", DateTime, nullable=False),
)
grants = Table(
    "bz_grants", meta,
    Column("user_id", String(200), primary_key=True),
    Column("source", String(100), primary_key=True),
    Column("expires_at", DateTime, nullable=True),        # NULL = không hết hạn
    Column("granted_by", String(200), nullable=False),
    Column("created_at", DateTime, nullable=False),
)
kv = Table(
    "bz_kv", meta,
    Column("key", String(200), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)      # lưu UTC dạng naive, giống nhau trên SQLite/Postgres


def new_thread_id() -> str:
    return uuid.uuid4().hex[:12]


class StateStore:
    def __init__(self, url: str):
        self.url = url
        u = make_url(url)
        if u.get_backend_name() == "sqlite" and u.database:
            Path(u.database).parent.mkdir(parents=True, exist_ok=True)
        args = {"check_same_thread": False} if u.get_backend_name() == "sqlite" else {}
        self.engine = create_engine(u, pool_pre_ping=True, connect_args=args)
        meta.create_all(self.engine)

    @classmethod
    def for_workspace(cls, ws: Workspace) -> StateStore:
        return cls(expand_env(ws.info.state.url, ws.env()))

    # ------------------------------------------------------------------ cuộc trò chuyện
    def touch(self, user_id: str, thread_id: str, title: str = "") -> None:
        now = _now()
        with self.engine.begin() as c:
            row = c.execute(select(threads.c.title).where(threads.c.user_id == user_id,
                                                          threads.c.thread_id == thread_id)).first()
            if row is None:
                c.execute(insert(threads).values(user_id=user_id, thread_id=thread_id, title=title[:200],
                                                 created_at=now, updated_at=now))
            else:
                values: dict[str, Any] = {"updated_at": now}
                if not row.title and title:
                    values["title"] = title[:200]
                c.execute(update(threads).where(threads.c.user_id == user_id, threads.c.thread_id == thread_id)
                          .values(**values))

    def list_threads(self, user_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(select(threads).where(threads.c.user_id == user_id)
                             .order_by(threads.c.updated_at.desc())).mappings().all()
        return [dict(r) for r in rows]

    def current_thread(self, user_id: str) -> str | None:
        rows = self.list_threads(user_id)
        return rows[0]["thread_id"] if rows else None

    def new_thread(self, user_id: str, title: str = "") -> str:
        tid = new_thread_id()
        self.touch(user_id, tid, title)
        return tid

    def delete_thread(self, user_id: str, thread_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(threads).where(threads.c.user_id == user_id, threads.c.thread_id == thread_id))

    def threads_to_prune(self, idle_days: int, max_per_user: int) -> list[tuple[str, str]]:
        """(user_id, thread_id) cần xoá: quá lâu không dùng, hoặc vượt số cuộc tối đa mỗi người."""
        cutoff = _now() - timedelta(days=idle_days)
        out: list[tuple[str, str]] = []
        with self.engine.connect() as c:
            out += [(r.user_id, r.thread_id) for r in c.execute(
                select(threads.c.user_id, threads.c.thread_id).where(threads.c.updated_at < cutoff))]
            heavy = c.execute(select(threads.c.user_id).group_by(threads.c.user_id)
                              .having(func.count() > max_per_user)).scalars().all()
            for uid in heavy:
                rows = c.execute(select(threads.c.thread_id).where(threads.c.user_id == uid)
                                 .order_by(threads.c.updated_at.desc())).scalars().all()
                out += [(uid, t) for t in rows[max_per_user:]]
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------ audit
    def audit(self, user_id: str, kind: str, **detail: Any) -> None:
        try:
            with self.engine.begin() as c:
                c.execute(insert(audit).values(ts=_now(), user_id=user_id or "?", kind=kind,
                                               detail=json.dumps(detail, ensure_ascii=False, default=str)[:4000]))
        except Exception as e:                       # audit lỗi không được làm hỏng câu trả lời
            log.error("Ghi audit lỗi: %s", e)

    def audit_rows(self, limit: int = 100, user_id: str | None = None) -> list[dict[str, Any]]:
        q = select(audit).order_by(audit.c.id.desc()).limit(limit)
        if user_id:
            q = q.where(audit.c.user_id == user_id)
        with self.engine.connect() as c:
            return [{**r, "detail": json.loads(r["detail"])} for r in c.execute(q).mappings()]

    def prune_audit(self, days: int) -> int:
        with self.engine.begin() as c:
            return c.execute(delete(audit).where(audit.c.ts < _now() - timedelta(days=days))).rowcount

    # ------------------------------------------------------------------ người dùng và quyền cấp riêng
    def attach(self, ws: Workspace) -> None:
        """Cho workspace đọc người dùng và quyền cấp riêng từ DB (role_of, sources_for, all_users)."""
        ws.user_provider = self.list_users
        ws.grant_provider = self.active_grants

    def list_users(self):
        from bizagent.workspace import UserEntry

        with self.engine.connect() as c:
            rows = c.execute(select(users_t).order_by(users_t.c.id)).mappings().all()
        return [UserEntry(id=r["id"], name=r["name"], role=r["role"], password_hash=r["password_hash"]) for r in rows]

    def upsert_user(self, user_id: str, role: str, name: str = "", password_hash: str | None = None,
                    created_by: str = "") -> None:
        with self.engine.begin() as c:
            values = {"name": name, "role": role}
            if password_hash is not None:
                values["password_hash"] = password_hash
            if c.execute(update(users_t).where(users_t.c.id == user_id).values(**values)).rowcount == 0:
                c.execute(insert(users_t).values(id=user_id, created_by=created_by, created_at=_now(),
                                                 password_hash=password_hash, name=name, role=role))

    def delete_user(self, user_id: str) -> bool:
        with self.engine.begin() as c:
            c.execute(delete(grants).where(grants.c.user_id == user_id))
            return c.execute(delete(users_t).where(users_t.c.id == user_id)).rowcount > 0

    def add_grant(self, user_id: str, source: str, granted_by: str, hours: float | None = None) -> datetime | None:
        expires = _now() + timedelta(hours=hours) if hours else None
        with self.engine.begin() as c:
            c.execute(delete(grants).where(grants.c.user_id == user_id, grants.c.source == source))
            c.execute(insert(grants).values(user_id=user_id, source=source, expires_at=expires,
                                            granted_by=granted_by, created_at=_now()))
        return expires

    def revoke_grant(self, user_id: str, source: str | None = None) -> int:
        q = delete(grants).where(grants.c.user_id == user_id)
        if source:
            q = q.where(grants.c.source == source)
        with self.engine.begin() as c:
            return c.execute(q).rowcount

    def list_grants(self, user_id: str | None = None, include_expired: bool = False) -> list[dict[str, Any]]:
        q = select(grants).order_by(grants.c.user_id, grants.c.source)
        if user_id:
            q = q.where(grants.c.user_id == user_id)
        if not include_expired:
            q = q.where((grants.c.expires_at.is_(None)) | (grants.c.expires_at > _now()))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def active_grants(self, user_id: str) -> list[str]:
        return [g["source"] for g in self.list_grants(user_id)]

    # ------------------------------------------------------------------ khoá-giá trị
    def kv_get(self, key: str) -> Any:
        with self.engine.connect() as c:
            v = c.execute(select(kv.c.value).where(kv.c.key == key)).scalar()
        return json.loads(v) if v is not None else None

    def kv_set(self, key: str, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, default=str)
        with self.engine.begin() as c:
            if c.execute(update(kv).where(kv.c.key == key).values(value=data, updated_at=_now())).rowcount == 0:
                c.execute(insert(kv).values(key=key, value=data, updated_at=_now()))

    def kv_prefix(self, prefix: str) -> dict[str, Any]:
        """Mọi khoá bắt đầu bằng `prefix` (vd "usage:2026-11-20:")."""
        with self.engine.connect() as c:
            rows = c.execute(select(kv.c.key, kv.c.value).where(kv.c.key.startswith(prefix, autoescape=True))).all()
        return {k: json.loads(v) for k, v in rows}

    def add_usage(self, day: str, user_id: str, tokens_in: int, tokens_out: int) -> dict[str, int]:
        """Cộng dồn mức dùng trong ngày của một người (trong một giao dịch). Trả về tổng mới."""
        key = f"usage:{day}:{user_id}"
        with self.engine.begin() as c:
            v = c.execute(select(kv.c.value).where(kv.c.key == key)).scalar()
            cur = json.loads(v) if v else {"asks": 0, "tokens_in": 0, "tokens_out": 0}
            cur = {"asks": cur["asks"] + 1, "tokens_in": cur["tokens_in"] + tokens_in,
                   "tokens_out": cur["tokens_out"] + tokens_out}
            data = json.dumps(cur)
            if c.execute(update(kv).where(kv.c.key == key).values(value=data, updated_at=_now())).rowcount == 0:
                c.execute(insert(kv).values(key=key, value=data, updated_at=_now()))
        return cur

    def prune_kv(self, prefix: str, days: int) -> int:
        """Xoá khoá `prefix...` không cập nhật quá `days` ngày (mức dùng cũ, link đăng nhập đã dùng...)."""
        with self.engine.begin() as c:
            return c.execute(delete(kv).where(kv.c.key.startswith(prefix, autoescape=True),
                                              kv.c.updated_at < _now() - timedelta(days=days))).rowcount

    def close(self) -> None:
        self.engine.dispose()


async def open_checkpointer(store: StateStore, stack: AsyncExitStack):
    """Checkpointer LangGraph lưu nội dung hội thoại cùng chỗ với StateStore (SQLite hoặc Postgres)."""
    u = make_url(store.url)
    backend = u.get_backend_name()
    if backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as e:
            raise RuntimeError("Cần langgraph-checkpoint-sqlite và aiosqlite") from e
        path = Path(u.database).with_name(Path(u.database).stem + "_checkpoints.db")
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(path)))
    elif backend == "postgresql":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as e:
            raise RuntimeError('Lưu phiên vào PostgreSQL cần: pip install "bizagent[postgres-state]"') from e
        dsn = u.set(drivername="postgresql").render_as_string(hide_password=False)
        saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
    else:
        raise RuntimeError(f"state.url: chưa hỗ trợ {backend} (dùng sqlite hoặc postgresql)")
    await saver.setup()
    return saver
