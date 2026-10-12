"""The adapter contract.

Every adapter has the same job: turn one source into candidate ``Transaction`` rows (and,
for a contract note, unresolved ``FnoParsedRow`` F&O trades) and stop. It does not value
anything, does not compute holdings, and -- importantly -- does not write to the ledger.
Staging and promotion are separate steps in ``db/imports.py``.

VALIDATION-FIRST. A row that cannot be parsed is rejected WITH ITS ROW NUMBER and the rest
still import. One malformed line in a 115-row export must not cost the other 114, and a
person fixing it needs to be told which line to look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.engine.fno_types import FnoSide, FnoTradeKind
from app.engine.types import Source, Transaction

#: A cleanly parsed structured row. Nothing was inferred.
CONFIDENCE_CERTAIN = Decimal("1.0")

#: Something was derived rather than read -- a charge apportioned across a multi-line
#: contract note, an ISIN resolved from a ticker. Still importable, worth a human's eye.
CONFIDENCE_DERIVED = Decimal("0.8")

#: Structurally sound but semantically doubtful. Should not promote without review.
CONFIDENCE_UNCERTAIN = Decimal("0.5")

#: Rows at or below this are surfaced by ``review_batch`` for explicit attention.
REVIEW_THRESHOLD = Decimal("0.9")


@dataclass(frozen=True, slots=True)
class StagedRow:
    """A candidate transaction, not yet in the ledger.

    ``confidence`` looks redundant for a tradebook, where everything is structured and
    scores 1.0. It exists so the contract-note parser -- where confidence genuinely varies
    -- slots into this same staging flow rather than inventing a second one, and so a human
    reviewing any batch reads one consistent signal.
    """

    row_number: int
    transaction: Transaction
    confidence: Decimal = CONFIDENCE_CERTAIN
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Rejection:
    """A row that could not be parsed. Carries the row number so it can be found."""

    row_number: int
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FnoParsedRow:
    """An F&O trade as a contract note prints it -- NOT yet resolved to a contract.

    docs/FNO_PLAN.md Piece 1. The parser stops here (CLAUDE.md rule 5): ``symbol`` is the
    display string (``NIFTY2481424200PE``), and turning it into an ``fno_contract`` id is
    ``app.db.imports``' job at staging time, through ``app.ingest.fno_symbol.resolve``.

    ``charges``/``stt`` are this trade's turnover share of the note's F&O charges only
    (rule 21(f)); ``stt`` is a subset of ``charges``. ``kind`` is EXCHANGE_SETTLEMENT for
    an exchange-generated expiry line (order no ``111111``).
    """

    row_number: int
    symbol: str
    exchange: str
    side: FnoSide
    quantity: Decimal
    price: Decimal
    trade_date: date
    trade_no: str
    order_no: str
    contract_number: str | None
    charges: Decimal
    stt: Decimal
    kind: FnoTradeKind = FnoTradeKind.TRADE
    source: Source = Source.CONTRACT_NOTE
    #: Charges are apportioned, never read per trade -- same claim as an equity
    #: contract-note row.
    confidence: Decimal = CONFIDENCE_DERIVED
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParseResult:
    rows: list[StagedRow] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: F&O trades, unresolved. Never mixed into ``rows``: an F&O trade is not a
    #: ``Transaction`` and never reaches ``transaction`` (rule 21(b)).
    fno_rows: list[FnoParsedRow] = field(default_factory=list)

    @property
    def parsed(self) -> int:
        return len(self.rows)

    @property
    def rejected(self) -> int:
        return len(self.rejections)


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    """Who a document says it belongs to, read off the document itself.

    Exists so a broker document can be checked against the account it is being staged
    into. An advisor runs many portfolios and picks the destination account from a
    dropdown; the inbound-email path resolves it from an address a stranger could guess.
    Both are ways for one investor's contract note to land in another's ledger, and the
    document names its own owner, so it can say when that has happened.

    This is ingestion-layer only -- plain strings read off the page, no hashing and no
    database. The comparison lives in ``app.db.identity``, which is where the pepper and
    the ``investor_secret`` row are already reachable.

    Both fields are optional because not every document carries both, and an adapter
    reports what it found rather than inventing what it did not. An empty field means
    "not stated on this document", never "did not match".
    """

    #: Permanent Account Number as printed. Never logged or surfaced unredacted --
    #: ``app.db.crypto.redact_pan`` exists for that.
    pan: str = ""

    #: The broker's own code for the client -- Zerodha calls it UCC. Checked against
    #: ``account.client_id``, which is where it is already stored.
    client_code: str = ""

    def __bool__(self) -> bool:
        """False when the document stated no identifier at all, so a caller can treat
        "nothing to check" and "no identity block" as the same thing."""
        return bool(self.pan or self.client_code)


@runtime_checkable
class Adapter(Protocol):
    """Layer 1. Produces candidate rows; never touches the ledger."""

    name: str

    def parse(self, content: bytes, account_id: str, filename: str = "") -> ParseResult: ...


@runtime_checkable
class IdentifyingAdapter(Protocol):
    """An adapter whose source names its owner on the document.

    Optional and deliberately separate from ``Adapter``: a Zerodha tradebook CSV carries
    no PAN, so requiring this of every adapter would mean either lying or a stub that
    returns nothing. ``app.db.imports.stage_batch`` checks for the method and skips the
    verification entirely when it is absent -- the same optional-protocol shape
    ``parse_declared_holdings`` already uses there.
    """

    name: str

    def identity(self, content: bytes) -> DocumentIdentity: ...
