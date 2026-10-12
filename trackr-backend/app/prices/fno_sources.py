"""Equity F&O bhavcopy: where it comes from, how it is parsed, one day at a time.

docs/FNO_PLAN.md Piece 2, docs/ARCHITECTURE_INVARIANTS.md rule 21. The cash-market pipe
(``app.prices.sources`` / ``udiff`` / ``pipe``) is the model; its HTTP fetch, browser
headers, 404-vs-HTML "nothing published" handling and legacy-archive warm-up are reused,
not copied.

Two tiers per exchange, dispatched by date exactly like the CM ``TieredBhavcopySource``:

* **UDiFF** (both exchanges, on/after ``NSE_CURRENT_FORMAT_SINCE`` = 2024-01-01). One
  34-column schema for NSE and BSE. Verified live 2026-10-08: NSE answers 2024-01-01 and
  later, 404s 2023-12-29; BSE answers 2024-01-02, and serves its HTML homepage for
  2023-12-29.
* **Legacy NSE** ``fo{DDMMMYYYY}bhav.csv.zip`` (before 2024-01-01). Verified live
  2026-10-08 for 2015-04-01, 2018-01-01, 2020-01-01, 2022-01-03, 2023-10-17 and 2024-07-05;
  older dates 403 without the warmed session cookie, so the CM warm-up is reused. It is
  re-shaped into UDiFF column names so ``parse`` stays the only F&O parser. On 2024-07-05,
  where both formats exist, close, settlement price and open interest agree row for row
  (see tests). The legacy file carries NO lot size, NO exchange token and NO underlying
  close; those stay None and are never inferred from value / contracts arithmetic.
* **Legacy BSE**: not built. A pre-2024 BSE date raises ``TierNotBuilt``, which the day
  fetch records as ``not_covered`` -- neither a holiday nor a price.

Every number is a ``Decimal``. A blank or zero price is ``None``, never zero (CLAUDE.md
rule 4). Only equity derivatives (``FinInstrmTp`` IDF, IDO, STF, STO) are kept.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.prices.alerts import LoggingNotifier, Notifier
from app.prices.sources import (
    _LEGACY_MONTH_ABBREVIATIONS,
    LEGACY_NSE_WARMUP_URL,
    NSE_CURRENT_FORMAT_SINCE,
    FetchFailed,
    HttpBhavcopySource,
    NotPublished,
    TieredBhavcopySource,
    _legacy_date,
    fetch_legacy_archive_text,
)
from app.prices.udiff import _decompress

NSE_FO_URL = (
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
)
BSE_FO_URL = (
    "https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_{d:%Y%m%d}_F_0000.CSV"
)
LEGACY_NSE_FO_URL = (
    "https://archives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{mon}/"
    "fo{d:%d}{mon}{d:%Y}bhav.csv.zip"
)

SOURCE_LABEL = {"NSE": "NSE_FO_BHAVCOPY", "BSE": "BSE_FO_BHAVCOPY"}
SOURCE_TYPE = "EXCHANGE_FO_BHAVCOPY"

FUTURES = frozenset({"IDF", "STF"})
OPTIONS = frozenset({"IDO", "STO"})
#: Equity derivatives only. Currency / commodity / anything else in the file is ignored.
FO_INSTRUMENTS = FUTURES | OPTIONS
OPTION_TYPES = frozenset({"CE", "PE"})

#: Legacy INSTRUMENT -> UDiFF FinInstrmTp. FUTIVX (India VIX futures) and anything else
#: is not an equity derivative and is dropped.
_LEGACY_INSTRUMENTS = {"FUTIDX": "IDF", "OPTIDX": "IDO", "FUTSTK": "STF", "OPTSTK": "STO"}

#: Columns ``parse`` cannot work without. The rest (FinInstrmId, UndrlygPric, OpnIntrst,
#: NewBrdLotQty) are optional because the legacy tier does not have them all.
REQUIRED_COLUMNS = frozenset(
    {"TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "ClsPric", "SttlmPric"}
)


class FnoFormatError(ValueError):
    """The file is not an F&O bhavcopy in the expected shape, or contradicts itself."""


class TierNotBuilt(Exception):
    """No archive tier exists for this exchange on this date. Not a holiday, not a price."""


@dataclass(frozen=True, slots=True)
class FnoRow:
    exchange: str
    instrument_type: str
    underlying: str
    expiry: date
    strike: Decimal | None
    option_type: str | None
    price_date: date
    close: Decimal | None
    settlement_price: Decimal | None
    underlying_close: Decimal | None
    open_interest: Decimal | None
    lot_size: int | None
    exchange_token: str | None
    #: ``FinInstrmNm`` -- the exchange's display symbol, the string a contract note prints.
    #: None from the legacy archive, which has no such column (migration 065).
    fin_instrm_nm: str | None = None

    @property
    def identity(self) -> tuple:
        return (
            self.exchange, self.underlying, self.instrument_type, self.expiry,
            self.strike, self.option_type,
        )


def _price(raw: str | None) -> Decimal | None:
    """A positive Decimal, or None for blank / zero. Never zero."""
    text = (raw or "").strip()
    if not text:
        return None
    value = Decimal(text)
    return value if value > 0 else None


def parse(payload: bytes, exchange: str) -> list[FnoRow]:
    """Every equity-derivative row of one F&O bhavcopy (zip or bare CSV).

    Raises:
        FnoFormatError: required columns missing, a row's ``Src`` names another exchange,
            an IDF/IDO/STF/STO row that does not parse, or the same contract twice.
    """
    reader = csv.DictReader(io.StringIO(_decompress(payload)))
    if reader.fieldnames is None:
        raise FnoFormatError(f"{exchange}: F&O file is empty")
    missing = REQUIRED_COLUMNS - {(f or "").strip() for f in reader.fieldnames}
    if missing:
        raise FnoFormatError(f"{exchange}: not an F&O UDiFF bhavcopy, missing {sorted(missing)}")

    rows: list[FnoRow] = []
    seen: set[tuple] = set()
    for line, raw in enumerate(reader, start=2):
        instrument = (raw.get("FinInstrmTp") or "").strip().upper()
        if instrument not in FO_INSTRUMENTS:
            continue
        src = (raw.get("Src") or "").strip().upper()
        if src and src != exchange:
            raise FnoFormatError(f"{exchange}: line {line} says Src={src}")
        try:
            strike = _price(raw.get("StrkPric"))
            option_type = (raw.get("OptnTp") or "").strip().upper() or None
            if instrument in FUTURES:
                if strike is not None or option_type not in (None, "XX"):
                    raise ValueError("future with a strike or option type")
                option_type = None
            elif strike is None or option_type not in OPTION_TYPES:
                raise ValueError("option without a strike or CE/PE")
            underlying = (raw.get("TckrSymb") or "").strip().upper()
            if not underlying:
                raise ValueError("no underlying")
            lot_text = (raw.get("NewBrdLotQty") or "").strip()
            lot_size = int(lot_text) if lot_text else None
            oi_text = (raw.get("OpnIntrst") or "").strip()
            row = FnoRow(
                exchange=exchange,
                instrument_type=instrument,
                underlying=underlying,
                expiry=date.fromisoformat((raw.get("XpryDt") or "").strip()),
                strike=strike,
                option_type=option_type,
                price_date=date.fromisoformat((raw.get("TradDt") or "").strip()),
                close=_price(raw.get("ClsPric")),
                settlement_price=_price(raw.get("SttlmPric")),
                underlying_close=_price(raw.get("UndrlygPric")),
                open_interest=Decimal(oi_text) if oi_text else None,
                lot_size=lot_size if lot_size and lot_size > 0 else None,
                exchange_token=(raw.get("FinInstrmId") or "").strip() or None,
                fin_instrm_nm=(raw.get("FinInstrmNm") or "").strip().upper() or None,
            )
        except (InvalidOperation, ValueError) as exc:
            raise FnoFormatError(f"{exchange}: line {line} unreadable ({exc})") from exc
        if row.identity in seen:
            raise FnoFormatError(f"{exchange}: contract listed twice {row.identity}")
        seen.add(row.identity)
        rows.append(row)
    return rows


def _legacy_fo_to_udiff_csv(legacy_text: str) -> bytes:
    """Re-shape NSE's pre-UDiFF ``fo{DDMMMYYYY}bhav.csv`` into UDiFF column names.

    Futures carry ``STRIKE_PR=0`` / ``OPTION_TYP=XX``; both become blank. Only columns the
    legacy file really has are produced -- no lot size, token or underlying close.
    """
    reader = csv.DictReader(io.StringIO(legacy_text))
    columns = {(f or "").strip() for f in (reader.fieldnames or [])}
    if not {"INSTRUMENT", "SYMBOL", "EXPIRY_DT", "TIMESTAMP"} <= columns:
        return b"Src\n"  # parse() then raises FnoFormatError for the missing columns
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "TradDt", "Src", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
        "ClsPric", "SttlmPric", "OpnIntrst",
    ])
    writer.writeheader()
    for raw in reader:
        instrument = _LEGACY_INSTRUMENTS.get((raw.get("INSTRUMENT") or "").strip().upper())
        if instrument is None:
            continue
        expiry = _legacy_date(raw.get("EXPIRY_DT") or "")
        traded = _legacy_date(raw.get("TIMESTAMP") or "")
        future = instrument in FUTURES
        writer.writerow({
            "TradDt": traded.isoformat() if traded else "",
            "Src": "NSE",
            "FinInstrmTp": instrument,
            "TckrSymb": (raw.get("SYMBOL") or "").strip(),
            "XpryDt": expiry.isoformat() if expiry else "",
            "StrkPric": "" if future else (raw.get("STRIKE_PR") or "").strip(),
            "OptnTp": "" if future else (raw.get("OPTION_TYP") or "").strip(),
            "ClsPric": (raw.get("CLOSE") or "").strip(),
            "SttlmPric": (raw.get("SETTLE_PR") or "").strip(),
            "OpnIntrst": (raw.get("OPEN_INT") or "").strip(),
        })
    return out.getvalue().encode("utf-8")


class LegacyNseFoSource:
    """NSE's pre-2024 F&O archive, with the CM legacy tier's warm-up and fetch rules."""

    exchange = "NSE"

    def __init__(self, opener_factory=None):
        self._opener_factory = opener_factory

    def url_for(self, on: date) -> str:
        return LEGACY_NSE_FO_URL.format(d=on, mon=_LEGACY_MONTH_ABBREVIATIONS[on.month - 1])

    def fetch(self, on: date) -> bytes:
        text = fetch_legacy_archive_text(
            "NSE F&O (legacy archive)", self.url_for(on), on,
            LEGACY_NSE_WARMUP_URL, self._opener_factory,
        )
        return _legacy_fo_to_udiff_csv(text)


class NoLegacyTier:
    """The seam for an archive tier that is not built (BSE F&O before UDiFF)."""

    def __init__(self, exchange: str):
        self.exchange = exchange

    def url_for(self, on: date) -> str:
        return ""

    def fetch(self, on: date) -> bytes:
        raise TierNotBuilt(f"{self.exchange} F&O: no pre-UDiFF archive tier; {on} not covered")


class TieredFoSource(TieredBhavcopySource):
    """The CM date dispatch, plus ``url_for`` so every stored price names its file."""

    def url_for(self, on: date) -> str:
        return (self._legacy if on < self._since else self._current).url_for(on)


def nse_fo_source(opener=None, legacy_opener_factory=None) -> TieredFoSource:
    return TieredFoSource(
        "NSE", HttpBhavcopySource("NSE", NSE_FO_URL, opener),
        LegacyNseFoSource(legacy_opener_factory), NSE_CURRENT_FORMAT_SINCE,
    )


def bse_fo_source(opener=None) -> TieredFoSource:
    return TieredFoSource(
        "BSE", HttpBhavcopySource("BSE", BSE_FO_URL, opener),
        NoLegacyTier("BSE"), NSE_CURRENT_FORMAT_SINCE,
    )


@dataclass
class FnoDayResult:
    price_date: date
    rows: dict[str, list[FnoRow]] = field(default_factory=dict)
    #: exchange -> provenance of the file its rows came from.
    receipts: dict[str, dict[str, str]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    not_published: list[str] = field(default_factory=list)
    not_covered: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return not self.failures


def fetch_fo_day(
    on: date, sources: list[Any] | None = None, notifier: Notifier | None = None
) -> FnoDayResult:
    """Fetch and parse each exchange's F&O bhavcopy for one date. Storage-free.

    A closed day is ``not_published`` and yields no rows -- never zero prices. A fetch or
    format failure, a file dated another day, or a published file with no equity-derivative
    rows is a failure and is alerted.
    """
    sources = sources if sources is not None else [nse_fo_source(), bse_fo_source()]
    notifier = notifier or LoggingNotifier()
    result = FnoDayResult(price_date=on)
    for source in sources:
        try:
            payload = source.fetch(on)
            rows = parse(payload, source.exchange)
            if not rows:
                raise FnoFormatError(f"{source.exchange}: F&O file for {on} has no F&O rows")
            dates = {r.price_date for r in rows}
            if dates != {on}:
                raise FnoFormatError(
                    f"{source.exchange}: asked for {on}, file is dated {sorted(dates)}"
                )
        except NotPublished:
            result.not_published.append(source.exchange)
            continue
        except TierNotBuilt as exc:
            result.not_covered[source.exchange] = str(exc)
            continue
        except (FetchFailed, FnoFormatError) as exc:
            result.failures[source.exchange] = str(exc)
            notifier.notify(
                subject=f"{source.exchange} F&O bhavcopy unavailable for {on}",
                detail=str(exc),
                context={"exchange": source.exchange, "date": on.isoformat()},
            )
            continue
        result.rows[source.exchange] = rows
        result.receipts[source.exchange] = {
            "source_type": SOURCE_TYPE,
            "source_url": source.url_for(on),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "publication_date": on.isoformat(),
            "period": on.isoformat(),
            "retrieved_at": datetime.now(UTC).isoformat(),
        }
    return result
