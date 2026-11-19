from datetime import date

import pytest

from bizagent.periods import PeriodError, resolve_period

TODAY = date(2026, 11, 19)  # thứ Năm


@pytest.mark.parametrize(
    "text,start,end",
    [
        ("hôm nay", "2026-11-19", "2026-11-19"),
        ("hôm qua", "2026-11-18", "2026-11-18"),
        ("Hom qua", "2026-11-18", "2026-11-18"),
        ("tuần này", "2026-11-16", "2026-11-19"),
        ("tuần trước", "2026-11-09", "2026-11-15"),
        ("tháng này", "2026-11-01", "2026-11-19"),
        ("tháng trước", "2026-10-01", "2026-10-31"),
        ("quý này", "2026-10-01", "2026-11-19"),
        ("quý trước", "2026-07-01", "2026-09-30"),
        ("năm ngoái", "2025-01-01", "2025-12-31"),
        ("7 ngày qua", "2026-11-12", "2026-11-18"),
        ("last 2 weeks", "2026-11-05", "2026-11-18"),
        ("tháng 10", "2026-10-01", "2026-10-31"),
        ("tháng 2/2024", "2024-02-01", "2024-02-29"),
        ("2026-09", "2026-09-01", "2026-09-30"),
        ("quý 1/2026", "2026-01-01", "2026-03-31"),
        ("5/11", "2026-11-05", "2026-11-05"),
        ("từ 1/11 đến 5/11", "2026-11-01", "2026-11-05"),
        ("2026-11-01..2026-11-07", "2026-11-01", "2026-11-07"),
    ],
)
def test_resolve(text, start, end):
    p = resolve_period(text, TODAY)
    assert (p.start.isoformat(), p.end.isoformat()) == (start, end)


def test_week_start_sunday():
    p = resolve_period("tuần này", TODAY, "sunday")
    assert p.start == date(2026, 11, 15)


def test_previous_same_length():
    prev = resolve_period("tuần trước", TODAY).previous()
    assert (prev.start, prev.end) == (date(2026, 11, 2), date(2026, 11, 8))


@pytest.mark.parametrize("text", ["", "hôm kìa nữa", "tháng 13", "từ 5/11 đến 1/11", "31/2/2026"])
def test_errors(text):
    with pytest.raises(PeriodError):
        resolve_period(text, TODAY)
