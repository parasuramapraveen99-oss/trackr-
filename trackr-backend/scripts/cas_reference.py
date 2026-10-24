"""Audit-only reference: Poppler layout text, independent of pdfplumber/app parsing.
No money/date is defaulted. Blank opening balances must follow a known closing balance.
"""

import re
from decimal import Decimal as D

N = r"(?:--|[\d,]+\.\d+)"
H = re.compile(
    r"^(IN[A-Z0-9]{9}\d)[*~#<>@$^]*\s+.*?("
    + N
    + r")\s+("
    + N
    + r")\s+("
    + N
    + r")\s+("
    + N
    + r")\s+("
    + N
    + r")\s+("
    + N
    + r")\s+("
    + N
    + r")\s*$"
)
M = re.compile(r"(\d{2}-\d{2}-\d{4})\s+((?:" + N + r"\s+){2,4})([\d,]+(?:\.\d+)?)\s*$")


def decimal(s):
    return D("0") if s == "--" else D(s.replace(",", ""))


def extract(text):
    holdings = {}
    movements = []
    last = None
    balances = {}
    lines = text.splitlines()
    # Poppler occasionally puts the date one physical line above a tall security row.
    # Join only a numeric-only ISIN row with a date immediately above it.
    for i, line in enumerate(lines):
        if i and re.match(
            r"^\s*IN[A-Z0-9]{9}\d[*~#<>@$^]*\s+(?:" + N + r"\s+){3}" + N + r"\s+\d+\s*$", line
        ):
            dm = re.search(r"(\d{2}-\d{2}-\d{4})\s*$", lines[i - 1])
            if dm:
                lead, tail = line.strip().split(None, 1)
                lines[i] = lead + " " + dm.group(1) + " " + tail
    for line in lines:
        line = line.strip()
        hm = H.match(line)
        if hm:
            isin, *numbers = hm.groups()
            numbers = list(map(decimal, numbers))
            if isin in holdings:
                raise ValueError("duplicate holdings " + isin)
            holdings[isin] = (numbers[0], numbers[-2], numbers[-1])
            continue
        mm = M.search(line)
        if not mm:
            continue
        im = re.match(r"^(IN[A-Z0-9]{9}\d)[*~#<>@$^]*(?:\s|$)", line)
        if im:
            last = im.group(1)
        if not last:
            raise ValueError("unattributed movement")
        nums = list(map(decimal, mm.group(2).split()))
        if len(nums) == 4:
            opening, credit, debit, closing = nums
        elif len(nums) == 3:
            credit, debit, closing = nums
            opening = balances.get(last)
        else:
            raise ValueError("unknown number count")
        if opening is None or opening + credit - debit != closing:
            raise ValueError("bad arithmetic " + last)
        balances[last] = closing
        movements.append((last, mm.group(1), credit, debit, closing))
    return holdings, movements
