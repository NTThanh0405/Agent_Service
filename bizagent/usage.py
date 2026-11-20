"""Báo cáo mức dùng token — CHỈ gửi khi người dùng yêu cầu (lệnh /token trong chat, `bizagent usage`),
không nằm trong báo cáo định kỳ.

    /token              mức dùng của chính mình: hôm nay, 7 ngày, 30 ngày
    /token <id>         (quản trị) mức dùng của một người, vd /token telegram:123
    /token all          (quản trị) tổng và những người dùng nhiều nhất, gồm cả nhận xét AI của báo cáo (report:<tên>)

Số liệu lấy từ state DB (khoá `usage:<ngày>:<người dùng>`, giữ 90 ngày), token do model báo về.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

WINDOWS = ((1, "Hôm nay"), (7, "7 ngày"), (30, "30 ngày"))
TOP = 15


class UsageError(Exception):
    pass


def _n(v: int) -> str:
    return f"{v:,}".replace(",", ".")


def load_usage(store, today: date, days: int = 30) -> dict[str, dict[str, dict[str, int]]]:
    """{người dùng: {ngày ISO: {asks, tokens_in, tokens_out}}} trong `days` ngày gần nhất (tính cả hôm nay)."""
    first = (today - timedelta(days=days - 1)).isoformat()
    out: dict[str, dict[str, dict[str, int]]] = {}
    for key, v in store.kv_prefix("usage:").items():
        _, day, uid = key.split(":", 2)
        if first <= day <= today.isoformat():
            out.setdefault(uid, {})[day] = v
    return out


def _sum(per_day: dict[str, dict[str, int]], today: date, days: int) -> dict[str, int]:
    first = (today - timedelta(days=days - 1)).isoformat()
    tot = {"asks": 0, "tokens_in": 0, "tokens_out": 0}
    for day, v in per_day.items():
        if day >= first:
            for k in tot:
                tot[k] += int(v.get(k, 0))
    return tot


def _user_block(uid: str, per_day: dict[str, dict[str, int]], today: date, daily_limit: int) -> str:
    lines = [f"Mức dùng token của {uid}:"]
    for days, label in WINDOWS:
        t = _sum(per_day, today, days)
        lines.append(f"• {label}: {_n(t['asks'])} lượt · vào {_n(t['tokens_in'])} · ra {_n(t['tokens_out'])} "
                     f"· tổng {_n(t['tokens_in'] + t['tokens_out'])}")
    if daily_limit and not uid.startswith("report:"):
        used = _sum(per_day, today, 1)
        used_total = used["tokens_in"] + used["tokens_out"]
        lines.append(f"Hạn mức mỗi ngày: {_n(daily_limit)} · hôm nay còn {_n(max(0, daily_limit - used_total))}")
    return "\n".join(lines)


def _all_block(data: dict[str, dict[str, dict[str, int]]], today: date) -> str:
    totals = {days: {"asks": 0, "tokens_in": 0, "tokens_out": 0} for days, _ in WINDOWS}
    for per_day in data.values():
        for days, _ in WINDOWS:
            for k, v in _sum(per_day, today, days).items():
                totals[days][k] += v
    lines = ["Tổng mức dùng token:"]
    for days, label in WINDOWS:
        t = totals[days]
        lines.append(f"• {label}: {_n(t['asks'])} lượt · tổng {_n(t['tokens_in'] + t['tokens_out'])} "
                     f"(vào {_n(t['tokens_in'])}, ra {_n(t['tokens_out'])})")
    ranked = sorted(((uid, _sum(d, today, 30)) for uid, d in data.items()),
                    key=lambda x: -(x[1]["tokens_in"] + x[1]["tokens_out"]))
    if ranked:
        lines.append(f"\nDùng nhiều nhất 30 ngày (tối đa {TOP}):")
        for i, (uid, t) in enumerate(ranked[:TOP], 1):
            today_t = _sum(data[uid], today, 1)
            lines.append(f"{i}. {uid} — {_n(t['tokens_in'] + t['tokens_out'])} "
                         f"({_n(t['asks'])} lượt; hôm nay {_n(today_t['tokens_in'] + today_t['tokens_out'])})")
    return "\n".join(lines)


def usage_report(ws, store, today: date, actor: tuple[str, str | None], args: list[str] | None = None) -> str:
    """Nội dung trả lời lệnh /token. `actor` = (user_id, role) của người gõ lệnh."""
    from bizagent.access import AccessError, is_admin, normalize_user_id

    actor_id, actor_role = actor
    args = args or []
    target = args[0] if args else actor_id
    if target != actor_id and not is_admin(ws, actor_role):
        raise UsageError("Bạn chỉ xem được mức dùng của chính mình")
    data = load_usage(store, today)
    limit = ws.agents.limits.daily_tokens_per_user
    if target.lower() == "all":
        return _all_block(data, today) if data else "Chưa có lượt dùng nào trong 30 ngày qua."
    if target != actor_id and not target.startswith("report:"):
        try:
            target = normalize_user_id(target, actor_id)
        except AccessError as e:
            raise UsageError(str(e)) from e
    return _user_block(target, data.get(target, {}), today, limit)


def usage_rows(store, today: date, days: int = 30) -> list[dict[str, Any]]:
    """Cho lệnh CLI: mỗi người một dòng tổng trong `days` ngày."""
    data = load_usage(store, today, days)
    rows = [{"user": uid, **_sum(d, today, days)} for uid, d in data.items()]
    return sorted(rows, key=lambda r: -(r["tokens_in"] + r["tokens_out"]))
