"""Company ratios from audited exchange filings. Pure, Decimal only.

Inputs are one audited annual statement (``exchange_financials``) and, for valuation, the
current price. Every ratio is A ÷ B over filed line items; docs/PORTFOLIO_HEALTH.md lists
them. A missing or non-meaningful input gives None, never zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)

#: Interest cover reported when a company has operating profit and no finance costs.
#: The health anchors top out at 8x, so any cover above that scores the same.
DEBT_FREE_COVER = Decimal(999)


@dataclass(frozen=True, slots=True)
class Statement:
    revenue: Decimal | None
    profit_before_tax: Decimal | None
    finance_costs: Decimal | None
    net_profit: Decimal | None
    equity: Decimal | None
    borrowings_current: Decimal | None
    borrowings_noncurrent: Decimal | None
    share_capital: Decimal | None
    face_value: Decimal | None
    basic_eps: Decimal | None


@dataclass(frozen=True, slots=True)
class Ratios:
    roce: Decimal | None
    roe: Decimal | None
    net_margin: Decimal | None
    debt_to_equity: Decimal | None
    interest_cover: Decimal | None
    pe: Decimal | None
    pb: Decimal | None


def _borrowings(s: Statement) -> Decimal | None:
    if s.borrowings_current is None or s.borrowings_noncurrent is None:
        return None
    return s.borrowings_current + s.borrowings_noncurrent


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None or b == ZERO else a / b


def ratios(s: Statement, price: Decimal | None) -> Ratios:
    """ROCE = EBIT ÷ (equity + borrowings); EBIT = profit before tax + finance costs.
    ROE = net profit ÷ equity (equity > 0 only). Net margin = net profit ÷ revenue.
    Debt/equity = borrowings ÷ equity.
    Interest cover = EBIT ÷ finance costs. P/E = price ÷ annual basic EPS (EPS > 0 only).
    P/B = price ÷ (equity ÷ shares), shares = share capital ÷ face value (equity > 0 only).
    """
    debt = _borrowings(s)
    ebit = (
        None
        if s.profit_before_tax is None or s.finance_costs is None
        else s.profit_before_tax + s.finance_costs
    )
    positive_equity = s.equity if s.equity is not None and s.equity > ZERO else None
    capital = None if positive_equity is None or debt is None else positive_equity + debt
    if ebit is not None and s.finance_costs == ZERO:
        cover = DEBT_FREE_COVER if ebit > ZERO else ZERO
    else:
        cover = _div(ebit, s.finance_costs)
    shares = _div(s.share_capital, s.face_value)
    book_per_share = _div(positive_equity, shares)
    eps = s.basic_eps if s.basic_eps is not None and s.basic_eps > ZERO else None
    return Ratios(
        roce=_div(ebit, capital),
        roe=_div(s.net_profit, positive_equity),
        net_margin=_div(s.net_profit, s.revenue) if s.revenue and s.revenue > ZERO else None,
        debt_to_equity=_div(debt, positive_equity),
        interest_cover=cover,
        pe=_div(price, eps),
        pb=_div(price, book_per_share),
    )
