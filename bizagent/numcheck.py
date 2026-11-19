"""Đối chiếu con số trong câu chữ (do AI viết) với số liệu thật (do tool/truy vấn trả về). Không gọi AI.

Hiểu cách viết số tiếng Việt và tiếng Anh: 1.234.567 · 1,234,567 · 41,5 triệu · 41.5tr · 1,2 tỷ · 12,3% · 850k.
Một số được coi là "khớp" nếu bằng (sai số làm tròn) một số trong dữ liệu, hoặc một phép tính đơn giản giữa hai số
trong dữ liệu: tỉ lệ %, % thay đổi, chênh lệch, tổng, thương (AOV...).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from itertools import permutations

UNITS = {"tỷ": 1e9, "ty": 1e9, "tỉ": 1e9, "triệu": 1e6, "trieu": 1e6, "tr": 1e6, "nghìn": 1e3, "ngàn": 1e3,
         "k": 1e3, "%": 1.0}
_DATES = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{1,2}:\d{2}\b")
_TOKEN = re.compile(r"(?<![\w.,])([-+−]?\d[\d.,]*\d|[-+−]?\d)(\s*(?:tỷ|tỉ|ty|triệu|trieu|tr|nghìn|ngàn|k|%)(?![\w]))?",
                    re.IGNORECASE)
_PLAIN = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class Mention:
    raw: str
    values: set[float]
    percent: bool
    decimals: int
    multiplier: float


def _interpretations(tok: str) -> list[tuple[float, int]]:
    """Các cách hiểu của một chuỗi số -> [(giá trị, số chữ số thập phân)]."""
    tok = tok.replace("−", "-")
    out: list[tuple[float, int]] = []
    if re.fullmatch(r"[-+]?\d{1,3}(\.\d{3})+(,\d+)?", tok):            # 1.234.567,5 (vi)
        a, _, d = tok.replace(".", "").partition(",")
        out.append((float(f"{a}.{d}" if d else a), len(d)))
    elif re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+(\.\d+)?", tok):          # 1,234,567.5 (en)
        a, _, d = tok.replace(",", "").partition(".")
        out.append((float(f"{a}.{d}" if d else a), len(d)))
    if re.fullmatch(r"[-+]?\d+,\d+", tok):                              # 41,5 (vi thập phân)
        a, d = tok.split(",")
        out.append((float(f"{a}.{d}"), len(d)))
    if re.fullmatch(r"[-+]?\d+\.\d+", tok):                             # 41.5 (en thập phân)
        out.append((float(tok), len(tok.split(".")[1])))
    if re.fullmatch(r"[-+]?\d+", tok):
        out.append((float(tok), 0))
    return out


def extract_mentions(text: str, min_value: float = 10) -> list[Mention]:
    text = _DATES.sub(" ", text)
    found: list[Mention] = []
    for m in _TOKEN.finditer(text):
        tok, unit = m.group(1), (m.group(2) or "").strip().lower()
        mult = UNITS.get(unit, 1.0)
        interp = _interpretations(tok)
        if not interp:
            continue
        percent = unit == "%"
        values = {v * mult for v, _ in interp}
        biggest = max(abs(v) for v in values)
        if not percent and unit == "" and all(v.is_integer() and 1900 <= v <= 2100 for v in values):
            continue                                                     # năm
        if biggest <= (0.5 if percent else min_value):          # % nhỏ vẫn soát (vd "giảm 7%")
            continue
        found.append(Mention(m.group(0).strip(), values, percent, max(d for _, d in interp), mult))
    return found


def _nums(s: str) -> list[float]:
    out = []
    for x in _PLAIN.findall(s):
        try:
            out.append(float(x))
        except ValueError:
            pass
    return out


def _groups(text: str) -> list[list[float]]:
    """Nhóm số có liên quan: từng dòng và từng cột của bảng "a | b" (định dạng tool trả về);
    văn bản khác (JSON...) coi cả khối là một nhóm nếu không quá nhiều số."""
    lines = [ln for ln in text.splitlines() if " | " in ln]
    if not lines:
        nums = _nums(text)
        return [nums] if len(nums) <= 40 else []
    rows = [[_nums(c) for c in ln.split(" | ")] for ln in lines]
    groups = [[v for cell in r for v in cell] for r in rows]
    width = max(len(r) for r in rows)
    for j in range(width):
        col = [v for r in rows if j < len(r) for v in r[j]]
        if len(col) > 1:
            groups.append(col[:200])
    return groups


def known_values(texts: list[str]) -> list[float]:
    """Số trong dữ liệu + phép tính đơn giản giữa các số CÙNG DÒNG hoặc CÙNG CỘT (tỉ lệ, % thay đổi, chênh lệch,
    tổng, thương, tổng cột, tỉ trọng). Không ghép số tuỳ ý, để số bịa không tình cờ "khớp"."""
    derived: set[float] = set()
    for t in texts:
        derived.update(_nums(t))
        for g in _groups(t):
            total = sum(g)
            derived.add(total)
            if g:
                derived.add(total / len(g))
            for a, b in permutations(g, 2):
                derived.update((a - b, a + b))
                if b:
                    derived.update((a / b * 100, (a - b) / b * 100, a / b))
            if total:
                derived.update(a / total * 100 for a in g)
    return sorted(derived)


def _near(sorted_vals: list[float], x: float, tol: float) -> bool:
    i = bisect.bisect_left(sorted_vals, x - tol)
    return i < len(sorted_vals) and sorted_vals[i] <= x + tol


def unverified(text: str, data_texts: list[str], min_value: float = 10) -> list[str]:
    """Các con số trong `text` không đối chiếu được với dữ liệu."""
    mentions = extract_mentions(text, min_value)
    if not mentions:
        return []
    known = known_values(data_texts)
    bad: list[str] = []
    for m in mentions:
        ok = False
        for v in m.values:
            step = 10 ** (-m.decimals) * m.multiplier                  # đơn vị chữ số cuối khi đã làm tròn
            tol = step * 0.51 if m.percent else max(step * 0.51, abs(v) * 0.002)
            if _near(known, v, tol) or _near(known, -v, tol):
                ok = True
                break
        if not ok:
            bad.append(m.raw)
    return list(dict.fromkeys(bad))
