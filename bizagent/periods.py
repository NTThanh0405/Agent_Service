"""Đổi cụm thời gian (tiếng Việt / tiếng Anh) ra khoảng ngày. Code tính, không để LLM đoán.

Quy ước:
- Khoảng ngày là [start, end] gồm cả hai đầu.
- "tuần này / tháng này / năm nay / quý này" tính TỚI HÔM NAY (kỳ chưa trọn).
- "N ngày qua / N ngày gần nhất / last N days" = N ngày trọn vẹn, kết thúc HÔM QUA.
- Tuần bắt đầu theo workspace (monday mặc định).

Hỗ trợ: hôm nay, hôm qua, tuần này/trước, tháng này/trước, quý này/trước, năm nay/trước (năm ngoái),
N ngày/tuần/tháng qua, "tháng 10", "tháng 10/2026", "2026-10", "quý 3/2026", một ngày (2026-11-05, 5/11/2026, 5/11),
khoảng "từ 1/11 đến 5/11", "2026-11-01..2026-11-05", "1/11 - 5/11".
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> Period:
        """Kỳ liền trước, cùng độ dài."""
        end = self.start - timedelta(days=1)
        return Period(end - timedelta(days=self.days - 1), end, f"kỳ trước của {self.label}")


def _norm(text: str) -> str:
    text = text.strip().lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text)


def _month_bounds(y: int, m: int) -> tuple[date, date]:
    if not 1 <= m <= 12:
        raise PeriodError(f"Tháng {m} không hợp lệ")
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def _week_start(d: date, week_start: str) -> date:
    offset = d.weekday() if week_start == "monday" else (d.weekday() + 1) % 7
    return d - timedelta(days=offset)


def _parse_day(token: str, today: date) -> date:
    token = token.strip()
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", token):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", token):
            return date(int(m[3]), int(m[2]), int(m[1]))
        if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", token):
            return date(today.year, int(m[2]), int(m[1]))
    except ValueError as e:
        raise PeriodError(f"Ngày không hợp lệ: {token}") from e
    raise PeriodError(f"Không hiểu ngày: {token}")


_UNIT = r"(ngay|tuan|thang|days?|weeks?|months?)"


def resolve_period(text: str, today: date, week_start: str = "monday") -> Period:
    raw = text.strip()
    t = _norm(raw)
    if not t:
        raise PeriodError("Cụm thời gian rỗng")

    # --- khoảng ngày tường minh
    m = re.fullmatch(r"(?:tu |from )?(.+?)\s*(?:den|toi|to|\.\.|->| - |–)\s*(.+)", t)
    if m and re.search(r"\d", m[1]) and re.search(r"\d", m[2]):
        start, end = _parse_day(m[1], today), _parse_day(m[2], today)
        if end < start:
            raise PeriodError("Ngày kết thúc trước ngày bắt đầu")
        return Period(start, end, raw)

    if t in ("hom nay", "today"):
        return Period(today, today, "hôm nay")
    if t in ("hom qua", "yesterday"):
        d = today - timedelta(days=1)
        return Period(d, d, "hôm qua")
    if t in ("hom kia",):
        d = today - timedelta(days=2)
        return Period(d, d, "hôm kia")

    if t in ("tuan nay", "this week"):
        return Period(_week_start(today, week_start), today, "tuần này")
    if t in ("tuan truoc", "tuan roi", "last week"):
        end = _week_start(today, week_start) - timedelta(days=1)
        return Period(end - timedelta(days=6), end, "tuần trước")

    if t in ("thang nay", "this month"):
        return Period(today.replace(day=1), today, "tháng này")
    if t in ("thang truoc", "thang roi", "last month"):
        y, mo = _add_months(today.year, today.month, -1)
        s, e = _month_bounds(y, mo)
        return Period(s, e, "tháng trước")

    q_now = (today.month - 1) // 3 + 1
    if t in ("quy nay", "this quarter"):
        return Period(date(today.year, 3 * q_now - 2, 1), today, "quý này")
    if t in ("quy truoc", "last quarter"):
        y, mo = _add_months(today.year, 3 * q_now - 2, -3)
        return Period(date(y, mo, 1), _month_bounds(y, mo + 2)[1], "quý trước")

    if t in ("nam nay", "this year"):
        return Period(date(today.year, 1, 1), today, "năm nay")
    if t in ("nam truoc", "nam ngoai", "last year"):
        return Period(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "năm trước")

    # --- N ngày/tuần/tháng qua
    m = re.fullmatch(rf"(\d+) {_UNIT} (?:qua|gan nhat|gan day|vua qua)", t) or re.fullmatch(
        rf"(?:last|past) (\d+) {_UNIT}", t
    )
    if m:
        n, unit = int(m[1]), m[2]
        if n <= 0:
            raise PeriodError("Số lượng phải lớn hơn 0")
        end = today - timedelta(days=1)
        if unit.startswith(("ngay", "day")):
            start = end - timedelta(days=n - 1)
        elif unit.startswith(("tuan", "week")):
            start = end - timedelta(days=7 * n - 1)
        else:
            y, mo = _add_months(end.year, end.month, -n)
            start = date(y, mo, min(end.day, calendar.monthrange(y, mo)[1])) + timedelta(days=1)
        return Period(start, end, raw)

    # --- tháng M[/YYYY], YYYY-MM
    m = re.fullmatch(r"(?:thang|month) (\d{1,2})(?:[/ -](?:nam )?(\d{4}))?", t) or None
    if m:
        y = int(m[2]) if m[2] else today.year
        s, e = _month_bounds(y, int(m[1]))
        return Period(s, e, raw)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if m:
        s, e = _month_bounds(int(m[1]), int(m[2]))
        return Period(s, e, raw)

    # --- quý Q[/YYYY]
    m = re.fullmatch(r"(?:quy|q|quarter) ?([1-4])(?:[/ -](?:nam )?(\d{4}))?", t)
    if m:
        q, y = int(m[1]), int(m[2]) if m[2] else today.year
        return Period(date(y, 3 * q - 2, 1), _month_bounds(y, 3 * q)[1], raw)

    # --- năm YYYY
    m = re.fullmatch(r"(?:nam |year )?(\d{4})", t)
    if m:
        y = int(m[1])
        return Period(date(y, 1, 1), date(y, 12, 31), raw)

    # --- một ngày
    m = re.fullmatch(r"(?:ngay )?([\d/.-]+)", t)
    if m:
        d = _parse_day(m[1], today)
        return Period(d, d, raw)

    raise PeriodError(
        f"Không hiểu cụm thời gian '{raw}'. Dùng ví dụ: hôm qua, tuần này, tháng 10/2026, 7 ngày qua, từ 1/11 đến 5/11"
    )
