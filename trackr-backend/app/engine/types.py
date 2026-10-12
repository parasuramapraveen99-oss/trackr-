"""Core object shapes for the engine.

These dataclasses mirror ``schema/*.schema.json`` field for field. That correspondence
is not a convention to be remembered -- ``tests/test_types_match_schema.py`` asserts it
on every run and fails if the two ever drift.

Money and quantity are ``Decimal``, always. Binary approximations lose paise, and a
reconciliation that is off by paise takes days to track down. Rates (XIRR, CAGR, ratios)
are ``Decimal`` here too; they cross the JSON boundary as numbers, converted via ``str``.

Nothing in this module reaches upward into the adapter or web layers, or into any
framework. The engine is pure functions over plain data, which is what makes the
fixtures runnable with zero infrastructure and a port to another language a mechanical
exercise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")


class TxnType(StrEnum):
    """Every ledger event. Quantity and cash effects are tabulated in DATA_MODEL.md.

    The groups that matter to the cost pool:
      - acquiring for cash: BUY, IPO_ALLOT, TRANSFER_IN  -> quantity up, pool up
      - disposing:          SELL, TRANSFER_OUT, BUYBACK  -> quantity down, pool down
                                                            PROPORTIONALLY
      - free shares:        BONUS, SPLIT_ADJ, DEMERGER_IN -> quantity up, pool UNCHANGED

    That third group is the whole mechanism by which cost basis self-corrects after a
    corporate action, and needs no ratio arithmetic -- EXCEPT a demerger's cost ratio,
    which the depository does not apply automatically (see docs/TAX_ENGINE.md "Corporate
    action tax nuances"). DEMERGER_COST_OUT / DEMERGER_COST_IN are the pair of later,
    paired adjustment rows that move cost basis from the original security to the
    demerged one once the scheme document's cost ratio is known -- quantity unchanged on
    both sides, ENGINE_SPEC.md section 11, produced by engine/demerger.py.
    """

    BUY = "BUY"
    SELL = "SELL"
    BONUS = "BONUS"
    SPLIT_ADJ = "SPLIT_ADJ"
    DEMERGER_IN = "DEMERGER_IN"
    DEMERGER_COST_OUT = "DEMERGER_COST_OUT"
    DEMERGER_COST_IN = "DEMERGER_COST_IN"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    IPO_ALLOT = "IPO_ALLOT"
    BUYBACK = "BUYBACK"
    DIVIDEND = "DIVIDEND"


#: Acquisitions that move cash out and add to the cost pool.
ACQUIRING_TYPES = frozenset({TxnType.BUY, TxnType.IPO_ALLOT, TxnType.TRANSFER_IN})

#: Disposals that move cash in and remove cost from the pool proportionally.
DISPOSING_TYPES = frozenset({TxnType.SELL, TxnType.TRANSFER_OUT, TxnType.BUYBACK})

#: Free shares. Quantity rises, the cost pool does not move, the average falls.
FREE_SHARE_TYPES = frozenset({TxnType.BONUS, TxnType.SPLIT_ADJ, TxnType.DEMERGER_IN})


class Source(StrEnum):
    """Which adapter produced a row.

    Recorded for forensics and import summaries. The engine must never branch on it --
    that boundary is what lets a new adapter be added in layer 1 without touching
    layers 2 to 4.
    """

    MANUAL = "MANUAL"
    CSV = "CSV"
    TRADEBOOK = "TRADEBOOK"
    CONTRACT_NOTE = "CONTRACT_NOTE"
    EMAIL = "EMAIL"
    CORP_ACTION = "CORP_ACTION"
    CAS = "CAS"
    # A broker's own funds/ledger statement -- the only routinely available document that
    # is EVIDENCE OF PAYMENT rather than of declaration, and therefore the only automated
    # path from a declared dividend to a REALISED one. Kept distinct from CORP_ACTION
    # (which is the declaration side) and from MANUAL (a human's assertion) because
    # invariant 18 turns on exactly that distinction. See migration 033.
    FUNDS_STATEMENT = "FUNDS_STATEMENT"


class Term(StrEnum):
    """Holding-period classification for a realised gain."""

    SHORT = "SHORT"
    LONG = "LONG"


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    BOTH = "BOTH"


class AssetClass(StrEnum):
    """All 30 codes from ``schema/asset_classes.json``.

    Seeded in full so the enum never needs a migration. Phase 1 BUILDS EXACTLY ONE: EQ.
    This list is an extension point, not a to-do list.
    """

    EQ = "EQ"
    MF_EQUITY = "MF_EQUITY"
    MF_DEBT = "MF_DEBT"
    SIF = "SIF"
    BND = "BND"
    NCD = "NCD"
    SGB = "SGB"
    FD = "FD"
    BNK = "BNK"
    CD = "CD"
    LN = "LN"
    PPF_EPF = "PPF_EPF"
    NPS_ULIP = "NPS_ULIP"
    INS = "INS"
    PO = "PO"
    REIT = "REIT"
    INVIT = "INVIT"
    GLD = "GLD"
    SLV = "SLV"
    JWL = "JWL"
    PR = "PR"
    ART = "ART"
    AIF = "AIF"
    PE = "PE"
    PMS = "PMS"
    US_EQ = "US_EQ"
    INTL_PE = "INTL_PE"
    STOCK_FNO = "STOCK_FNO"
    OTHER_FNO = "OTHER_FNO"
    CRYPTO = "CRYPTO"


@dataclass(frozen=True, slots=True)
class Transaction:
    """One ledger row. The only thing in this system that is written.

    APPEND-ONLY. There is no correct way to edit one of these. A mistake is fixed by
    appending a reversing row and then the correct row, which is what keeps every
    number reproducible by replaying the ledger.

    ``quantity`` is always positive; direction comes from ``txn_type``.
    """

    isin: str
    txn_date: date
    txn_type: TxnType
    quantity: Decimal
    price: Decimal
    charges: Decimal = ZERO
    stt: Decimal = ZERO
    # NET dividend received -- what reached the bank, after any tax withheld at source.
    # This is the figure app.engine.xirr takes as a positive cash flow and
    # app.engine.twrr as a sub-period outflow, because both model money actually moving.
    dividend_amount: Decimal = ZERO
    # Tax withheld at source on a dividend, as recorded on a document. NONE MEANS UNKNOWN,
    # not zero -- zero asserts no tax was withheld, which is a different and checkable
    # claim (true below the section 194 threshold, true against a valid 15G/15H). Neither
    # corporate-action feed carries tax at all, so unknown is the honest default and the
    # only one that does not make a false assertion about every existing row.
    #
    # Gross is dividend_amount + (tds_amount or 0), DERIVED -- see gross_dividend below.
    # Storing all three would store one fact twice and invite them to disagree.
    tds_amount: Decimal | None = None
    cost_transfer: Decimal = ZERO
    id: str | None = None
    account_id: str | None = None
    source: Source | None = None
    source_ref: str | None = None
    dedupe_key: str | None = None
    raw_payload: dict[str, Any] | None = None
    created_at: datetime | None = None

    @property
    def gross_dividend(self) -> Decimal:
        """Gross dividend: net plus whatever tax was withheld.

        Derived rather than stored, so the three figures cannot drift apart. Equals
        ``dividend_amount`` when ``tds_amount`` is unknown -- which is NOT a claim that no
        tax was withheld, only the best available figure. A caller that needs to
        distinguish "gross, known" from "gross, assumed equal to net" must test
        ``tds_amount is None`` itself; this property deliberately does not encode that
        distinction in a sentinel.
        """
        return self.dividend_amount + (self.tds_amount or ZERO)


@dataclass(frozen=True, slots=True)
class Holding:
    """A current position. DERIVED, NEVER STORED -- recomputed on every read.

    ``avg_cost`` is ``invested / quantity``. Because a disposal removes cost
    proportionally, selling never moves it: a sale changes what you own, not what the
    shares you kept cost you.
    """

    isin: str
    quantity: Decimal
    avg_cost: Decimal
    invested: Decimal
    symbol: str | None = None
    last_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealised: Decimal | None = None
    unrealised_pct: Decimal | None = None
    weight_pct: Decimal | None = None
    first_buy_date: date | None = None
    cost_estimated: bool = False
    # ENGINE_SPEC section 5: cagr = (end_value / start_value)^(365.25/days) - 1, valid
    # only as a single-lump-sum approximation. For a position built from several buys,
    # start_value is the current weighted-average `invested` cost pool and start_date is
    # `first_buy_date` -- the earliest acquisition -- treating the whole blended cost as
    # if it had all been put in on that first date. None whenever days, invested, or a
    # price is unavailable; never defaulted to zero. Computed in engine/valuation.py
    # alongside market_value, since it needs a price and an as_of date, neither of which
    # compute_holdings has.
    cagr: Decimal | None = None
    # FIFO cost of the surviving lots, alongside the weighted-average headline. The two
    # differ only where a position has been partly sold, and then they differ a lot: on
    # KDL-SM in the golden fixture, by 8,144.32. Brokers report FIFO; ENGINE_SPEC section 1
    # displays weighted average. Carrying both is what lets a portfolio be reconciled
    # against a broker statement without either number being wrong.
    fifo_cost: Decimal | None = None
    fifo_avg: Decimal | None = None
    # Move since the previous close: (last_price - previous_close) / previous_close.
    # A DAY's performance, not a holding-period one -- unrealised_pct above answers
    # "since I bought", this answers "since yesterday", and the two are unrelated
    # numbers that happen to look alike. None -- never zero -- whenever there is no
    # previous close to compare against (a position priced for the first time today,
    # or a scrip the archive has only one close for); a zero would claim the price
    # did not move, which is a different statement from not knowing. Computed in
    # engine/valuation.py, which is the only place a price is allowed to meet a holding.
    day_change_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Lot:
    """One open FIFO lot. DERIVED, NEVER STORED.

    Runs alongside weighted-average cost: the average is what the dashboard shows, the
    lots are what tax computation needs.
    """

    isin: str
    open_date: date
    quantity_open: Decimal
    cost_per_unit: Decimal
    # The tax acquisition date's evidence for a BONUS lot (``open_date``), one of
    # AcquisitionDateBasis. None for every other lot, whose open_date is its own trade
    # or corporate-action date. ENGINE_SPEC section 3.
    acquisition_date_basis: str | None = None


class AcquisitionDateBasis(StrEnum):
    """Where a BONUS lot's acquisition date came from -- ENGINE_SPEC section 3, OD-1.

    ALLOTMENT is evidenced. The two PROVISIONAL values are evidenced lower bounds
    (allotment legally follows the record date, and the record date never precedes the
    ex-date), so the true acquisition date can only be the same or later.
    """

    ALLOTMENT = "ALLOTMENT"
    PROVISIONAL_RECORD_DATE = "PROVISIONAL_RECORD_DATE"
    PROVISIONAL_EX_DATE = "PROVISIONAL_EX_DATE"


@dataclass(frozen=True, slots=True)
class Realised:
    """A slice of a disposal matched against ONE lot. DERIVED, NEVER STORED.

    A sell spanning two lots produces two of these, which is exactly what the FIFO case
    in the fixtures asserts.
    """

    quantity: Decimal
    proceeds: Decimal
    cost: Decimal
    gain: Decimal
    term: Term
    isin: str | None = None
    sell_date: date | None = None
    holding_days: int | None = None
    # Section 48 (Income Tax Act) disallows STT as a deductible expense for capital
    # gains, unlike `cost`/`proceeds` above which stay net of ALL charges including STT
    # for Phase 1's real-cash display. app.engine.realised.tax_cost/tax_proceeds use
    # these to exclude it. Zero for every disposal except one drawing on
    # contract-note-derived charges (app.ingest.zerodha_contract_note is the only
    # adapter that ever populates Transaction.stt).
    cost_stt: Decimal = ZERO
    proceeds_stt: Decimal = ZERO
    # The matched lot's Lot.acquisition_date_basis, carried so tax output can say when
    # holding_days rests on a provisional bonus acquisition date. None otherwise.
    acquisition_date_basis: str | None = None
    # True when this slice's STCG/LTCG or grandfathering classification would change if
    # a PROVISIONAL bonus acquisition date moved forward by up to
    # BONUS_ALLOTMENT_SENSITIVITY_DAYS. compute_realised leaves it False: the window is
    # config-owned (app.config) and the engine never reads config, so the API layer sets
    # it from app.engine.tax_classification.classification_sensitive (see
    # app/routers/transactions.py). Mirrors schema/realised.schema.json
    # classification_sensitive; RealisedOut carries the same field.
    classification_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Portfolio totals as at one date.

    A persisted snapshot is a cache of a derivation, never a source of truth. It must
    always stay recomputable from the ledger.
    """

    as_of: date
    invested: Decimal
    market_value: Decimal
    unrealised: Decimal | None = None
    realised_to_date: Decimal | None = None
    # ISINs these totals do NOT include, because their own ledger segment did not
    # reconcile on this date (see compute_snapshot). Empty on a clean replay, which is
    # the normal case. The totals above are still real figures over everything that did
    # reconcile -- this field is what stops them being read as the whole portfolio when
    # they are not, and is why excluding a broken holding is not the same thing as
    # silently dropping one.
    excluded_isins: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DividendYear:
    """One ISIN's dividend total for one Indian financial year, e.g. ``"2025-26"``.

    Nested inside ``DividendSummary.by_year`` only -- like ``Benchmark`` inside
    ``Performance``, it has no top-level schema file of its own; its shape is the
    ``by_year`` items in ``dividend.schema.json``.
    """

    fy: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class DividendSummary:
    """One ISIN's lifetime dividend total, broken out by financial year. DERIVED, NEVER
    STORED -- computed in ``app.engine.dividends`` from DIVIDEND rows already flowing
    through XIRR (ENGINE_SPEC section 4) as a positive cash flow; this is the same rows'
    display aggregation, not a new calculation.

    An ISIN absent from a ``compute_dividends`` result has never recorded a DIVIDEND --
    a true, known zero, not a value substituted for a required one.
    """

    isin: str
    total: Decimal
    by_year: list[DividendYear]


@dataclass(frozen=True, slots=True)
class Contribution:
    """One ISIN's rupee contribution to the portfolio's total gain over a period.
    DERIVED, NEVER STORED -- ENGINE_SPEC section 10.

    ``gain = (end_value - start_value) - net_flow``, the per-holding mirror of section
    5's TWRR flow adjustment, expressed as a rupee amount rather than a ratio and not
    annualised. ``net_flow`` is BUY/IPO_ALLOT/TRANSFER_IN minus SELL/TRANSFER_OUT/
    BUYBACK minus DIVIDEND, dated within the window -- so a mid-period buy or a full
    mid-period exit counts only the gain earned while the position existed inside the
    window, never the whole lifetime figure.

    ``pct_of_total`` is ``gain / total_gain`` (``total_gain`` = the sum of every
    ``Contribution.gain`` in the result). ``None`` only when ``total_gain`` is exactly
    zero; shown uncapped otherwise, including negative and above-100% values when
    holdings offset -- that is correct, not a bug, per section 10.
    """

    isin: str
    start_value: Decimal
    end_value: Decimal
    net_flow: Decimal
    gain: Decimal
    pct_of_total: Decimal | None = None
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One mismatch between computed holdings and a declared quantity.

    A hypothesis for a human to confirm -- never an instruction to auto-correct the
    ledger.
    """

    isin: str
    computed_qty: Decimal
    declared_qty: Decimal
    difference: Decimal
    ratio: Decimal | None = None
    likely_cause: str | None = None


@dataclass(frozen=True, slots=True)
class Benchmark:
    """Cash-flow-replicated index return.

    Answers "what if I had bought the index on exactly the days I bought stocks".
    Comparing portfolio XIRR against a headline index CAGR is not a valid comparison
    and must not be shown.
    """

    name: str
    xirr: Decimal | None = None
    terminal_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Performance:
    """The performance response.

    BOTH XIRR VARIANTS ARE MANDATORY TOGETHER. They answer different questions, and the
    gap between them is itself the insight. Reporting one alone leaves a choice about
    which number flatters the portfolio; that choice should not exist.
    """

    as_of: date
    # Deliberately WITHOUT defaults: you cannot construct a Performance without
    # supplying both variants. The "never report one alone" rule is structural here,
    # not a convention someone has to remember. Either may still be None -- the solver
    # legitimately fails sometimes -- but the caller must say so explicitly.
    xirr_all: Decimal | None
    xirr_holdings: Decimal | None
    # FY-to-date variant, WITHOUT a default for the same reason: the caller must always
    # state it, even as None when no 1-April price exists for a held ISIN -- see
    # compute_fy_xirr's docstring, which forbids a substitute for that missing price.
    fy_xirr: Decimal | None
    # Fixed-calendar trailing windows, same resolution as fy_xirr (see
    # app.routers.analytical._window_xirr) -- 1/3/5 calendar years back from as_of.
    # Independently None: a portfolio with 2 years of history gets xirr_5y=None
    # alongside real xirr_1y/xirr_3y values, never all blanked together.
    xirr_1y: Decimal | None = None
    xirr_3y: Decimal | None = None
    xirr_5y: Decimal | None = None
    # TWRR (app.engine.twrr, ENGINE_SPEC section 5) -- the adviser-skill companion to
    # the four windows above, same target_start dates, same independent None-ness.
    # Never a default beyond None: a window with fewer than two usable snapshots or no
    # anchor within staleness tolerance is None, not omitted.
    fy_twrr: Decimal | None = None
    twrr_1y: Decimal | None = None
    twrr_3y: Decimal | None = None
    twrr_5y: Decimal | None = None
    absolute_return: Decimal | None = None
    cagr: Decimal | None = None
    invested: Decimal | None = None
    market_value: Decimal | None = None
    unrealised: Decimal | None = None
    realised_to_date: Decimal | None = None
    benchmark: Benchmark | None = None
    # Adviser-onboarding variant: money-weighted return since investor.onboarding_date
    # (docs/DATA_MODEL.md), resolved the same honest way fy_xirr is -- see
    # app.routers.analytical._window_xirr. period_start echoes back the date actually
    # used so a caller never has to guess which onboarding_date backed the percentage.
    period_xirr: Decimal | None = None
    period_start: date | None = None
    warning: str | None = None
    # ISINs left out of every figure above because their history does not reconcile yet
    # or they have no usable price (ARCHITECTURE_INVARIANTS rules 16, 20). Empty when
    # nothing was left out; the API carries the names and reasons beside them.
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """Result of appending rows to the ledger.

    Duplicates are SKIPPED and COUNTED, never treated as errors -- re-importing a file
    must be a no-op, and the caller still needs an honest summary to display.
    """

    inserted: int = 0
    skipped_duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
