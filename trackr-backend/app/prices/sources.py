"""Where bhavcopy files come from.

The URL shapes live here, in one place, because they are the single most likely thing to
break -- an exchange reorganising its archive is not a rare event, and a pipe that silently
starts 404ing every day is worse than one that fails loudly on day one.
"""

from __future__ import annotations

import csv
import io
import ssl
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Protocol, runtime_checkable

NSE_URL = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
)
BSE_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
)

#: The pre-2024 NSE archive -- a different URL, a different CSV shape (no UDiFF headers),
#: still genuinely free. See LegacyNseBhavcopySource for why this needs a warmed session
#: cookie where the current-format host does not.
LEGACY_NSE_URL = (
    "https://archives.nseindia.com/content/historical/EQUITIES/{d:%Y}/{mon}/"
    "cm{d:%d}{mon}{d:%Y}bhav.csv.zip"
)
LEGACY_NSE_WARMUP_URL = "https://www.nseindia.com/all-reports"

#: BSE's own pre-UDiFF archive. The ISIN-coded variant specifically -- BSE also publishes
#: an EQ{DDMMYY} file keyed on scrip code alone, which would have reintroduced the
#: identifier problem CLAUDE.md opens with. Confirmed live: this still serves October 2023,
#: which is the only place GUJTHEM's (BSE scrip 506879) split evidence exists at all.
LEGACY_BSE_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_{d:%d%m%y}.zip"
)

#: The date NSE_URL's exact filename scheme starts answering -- see
#: app.prices.pipe.NSE_BHAVCOPY_EARLIEST's docstring for how this was verified. Before
#: this, NseBhavcopySource goes to the legacy archive instead; on/after it, the 404 vs
#: 200 boundary is real news (a holiday), not a format problem, so nothing before this
#: date should ever be asked of the current-format URL.
NSE_CURRENT_FORMAT_SINCE = date(2024, 1, 1)

#: NSE returns 403 to anything that does not look like a browser.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/",
}

#: LEGACY_NSE_URL's host (archives.nseindia.com) 403s/503s BROWSER_HEADERS above even
#: with a Referer -- confirmed live. This fuller, more current header set plus an actual
#: warmed session cookie (LegacyNseBhavcopySource.fetch, not just a Referer string) is
#: what a real fetch of a real 17-Oct-2023 file needed to get past it.
LEGACY_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": LEGACY_NSE_WARMUP_URL,
}

TIMEOUT_SECONDS = 30


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """A context with an explicit CA bundle.

    Python does not use the macOS keychain and a fresh python.org install has no
    certificates at all, so urllib fails CERTIFICATE_VERIFY_FAILED on hosts where curl
    works fine. Containers have the same gap. Pointing at certifi makes the fetch behave
    identically everywhere.

    VERIFICATION IS NEVER DISABLED. Turning it off would "fix" this in one line and leave
    the pipe accepting any certificate for the rest of its life - on a feed that sets every
    price this system reports.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class NotPublished(Exception):
    """No bhavcopy for that date.

    NOT a failure. Weekends and exchange holidays have no file, and treating their absence
    as an outage either floods a mailbox every Sunday or -- far worse -- trains whoever
    reads the alerts to ignore them, which is how a genuine multi-day outage goes unnoticed.
    """


class FetchFailed(Exception):
    """The file should exist and could not be retrieved. This one alerts."""


@runtime_checkable
class BhavcopySource(Protocol):
    exchange: str

    def fetch(self, on: date) -> bytes: ...


class HttpBhavcopySource:
    def __init__(self, exchange: str, url_template: str, opener=None):
        self.exchange = exchange
        self.url_template = url_template
        self._opener = opener or urllib.request.urlopen

    def url_for(self, on: date) -> str:
        return self.url_template.format(d=on)

    def fetch(self, on: date) -> bytes:
        request = urllib.request.Request(self.url_for(on), headers=BROWSER_HEADERS)
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # 404 is the exchange saying "no trading that day". Anything else is a problem
            # with the fetch itself: 403 means the anti-bot headers stopped working, 5xx
            # means their side is down. Those must be distinguishable.
            if exc.code == 404:
                raise NotPublished(f"{self.exchange}: nothing published for {on}") from exc
            raise FetchFailed(f"{self.exchange}: HTTP {exc.code} for {on}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchFailed(f"{self.exchange}: {type(exc).__name__} for {on}") from exc

        # BSE's "no trading that day" answer is not a 404 -- confirmed live, 2026-08-30
        # (a Sunday): HTTP 200, its own marketing homepage HTML. Treated as NotPublished,
        # the same as NSE's 404, rather than falling through to parse() where it would
        # raise BhavcopyFormatError and alert every single non-trading day -- exactly the
        # "trains whoever reads the alerts to ignore them" failure NotPublished's own
        # docstring warns about. A real UDiFF file, zipped or bare CSV, never starts with
        # an HTML doctype/tag, so this cannot mask an actual format break.
        if body.lstrip()[:20].lower().startswith((b"<!doctype html", b"<html")):
            raise NotPublished(f"{self.exchange}: nothing published for {on} (HTML response)")
        return body


@dataclass(frozen=True, slots=True)
class _LegacyLayout:
    """Where a pre-UDiFF bhavcopy keeps the four things ``app.prices.udiff.parse`` reads.

    Both exchanges published their own shape before converging on UDiFF, and they differ
    in every column name, in how equity is marked, and even in the year width. Describing
    that as data rather than as two near-identical functions is what keeps ONE normaliser
    -- and therefore one parser -- for every source this system has.
    """

    isin: str
    close: str
    trade_date: str
    symbol: str
    #: The column that says what KIND of instrument a row is, and the values that mean
    #: cash-segment equity. Not cosmetic: both files carry other instruments whose ISINs
    #: look almost identical to their equity siblings.
    instrument_column: str
    equity_values: frozenset[str]
    #: Rupee turnover column, carried through as UDiFF's TtlTrfVal for liquidity.
    traded_value: str = ""


#: NSE's ``cmDDMMMYYYYbhav`` -- SERIES EQ (normal) and BE (trade-to-trade), both equity.
NSE_LEGACY_LAYOUT = _LegacyLayout(
    isin="ISIN",
    close="CLOSE",
    trade_date="TIMESTAMP",
    symbol="SYMBOL",
    instrument_column="SERIES",
    equity_values=frozenset({"EQ", "BE"}),
    traded_value="TOTTRDVAL",
)

#: BSE's ``EQ_ISINCODE_DDMMYY`` -- SC_TYPE 'Q' is equity; the same file also carries 'P'
#: (preference) and debt/bond rows. Filtering is load-bearing rather than tidiness: a
#: preference ISIN differs from its equity sibling only in the series digits
#: (INE919I04010 against an ...01... equity), so an unfiltered row would silently supply
#: the wrong instrument's price -- the same trap as the NCRPS false positive
#: app.ingest.corporate_action_feed already guards against.
BSE_LEGACY_LAYOUT = _LegacyLayout(
    isin="ISIN_CODE",
    close="CLOSE",
    trade_date="TRADING_DATE",
    symbol="SC_NAME",
    instrument_column="SC_TYPE",
    equity_values=frozenset({"Q"}),
    traded_value="NET_TURNOV",
)

_LEGACY_MONTH_ABBREVIATIONS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


def _legacy_date(raw: str) -> date | None:
    """``17-OCT-2023`` (NSE) or ``09-Oct-23`` (BSE) -> a real date, or None if neither.

    The two-digit year is BSE's, and these archives only exist for the 2000s, so the
    century is not a guess -- there is no 1923 bhavcopy to be ambiguous with.
    """
    try:
        day_s, mon_s, year_s = raw.strip().upper().split("-")
        year = int(year_s)
        if len(year_s) == 2:
            year += 2000
        return date(year, _LEGACY_MONTH_ABBREVIATIONS.index(mon_s) + 1, int(day_s))
    except (ValueError, IndexError):
        return None


def _to_udiff_csv(legacy_text: str, layout: _LegacyLayout = NSE_LEGACY_LAYOUT) -> bytes:
    """Re-shape a pre-UDiFF bhavcopy into UDiFF's column names, so
    ``app.prices.udiff.parse`` -- the one bhavcopy parser -- can read it unmodified.
    Only the columns that parser actually reads are produced; nothing here does its own
    Decimal/date validation, which happens exactly once, inside ``parse``, same as for
    any other source.

    Confirmed against real fetched files from both exchanges: the legacy formats DO carry
    an ISIN column, contradicting ``app.prices.udiff``'s old module docstring, which this
    change corrects. Proven equivalent, too -- on 2024-01-02, where both BSE formats are
    still published, all 3,975 equity closes agree exactly (see tests).
    """
    reader = csv.DictReader(io.StringIO(legacy_text))
    if reader.fieldnames is None:
        return b""
    columns = {(f or "").strip() for f in reader.fieldnames}
    if {layout.isin, layout.close, layout.trade_date} - columns:
        # Let the caller's udiff.parse raise BhavcopyFormatError with its own message --
        # an empty-but-headered UDiFF CSV reliably triggers that path.
        return b"ISIN,ClsPric,TradDt\n"

    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=["ISIN", "TckrSymb", "SctySrs", "ClsPric", "TradDt", "FinInstrmTp", "TtlTrfVal"],
    )
    writer.writeheader()
    for raw in reader:
        instrument = (raw.get(layout.instrument_column) or "").strip().upper()
        if instrument not in layout.equity_values:
            continue
        trad_dt = _legacy_date(raw.get(layout.trade_date) or "")
        if trad_dt is None:
            continue  # let udiff.parse's own per-row guards handle anything malformed
        writer.writerow(
            {
                "ISIN": (raw.get(layout.isin) or "").strip(),
                "TckrSymb": (raw.get(layout.symbol) or "").strip(),
                "SctySrs": instrument,
                "ClsPric": (raw.get(layout.close) or "").strip(),
                "TradDt": trad_dt.isoformat(),
                "FinInstrmTp": "EQ",
                "TtlTrfVal": (
                    (raw.get(layout.traded_value) or "").strip() if layout.traded_value else ""
                ),
            }
        )
    return out.getvalue().encode("utf-8")


def _legacy_opener():
    """A cookie-persisting opener -- the one thing that actually mattered. The plain
    ``urllib.request.urlopen`` HttpBhavcopySource uses (a fresh, cookie-less request
    every call) 403s/503s against archives.nseindia.com even with LEGACY_BROWSER_HEADERS;
    only a warmed session cookie, carried across the warmup and the real request via the
    SAME opener, got a real 200. Confirmed live."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        urllib.request.HTTPCookieProcessor(),
    )


class LegacyBhavcopySource:
    """A pre-UDiFF archive from either exchange, normalised to UDiFF bytes on the way out.

    One class, two exchanges: they differ in URL, in CSV layout, and in whether a warm-up
    request is needed, and all three are constructor arguments rather than a second
    near-identical class. Never constructed directly by a caller -- NseBhavcopySource and
    BseBhavcopySource pick it over the current-format source by date.

    ``warmup_url`` is NSE-only and load-bearing there: archives.nseindia.com 403s/503s a
    cold request even with browser headers, and only a session cookie carried across the
    warm-up and the real fetch by the SAME opener gets a 200. BSE needs no such dance, so
    it passes None and one request is made instead of two.
    """

    def __init__(
        self,
        exchange: str,
        url_template: str,
        layout: _LegacyLayout,
        warmup_url: str | None = None,
        opener_factory=None,
    ):
        self.exchange = exchange
        self.url_template = url_template
        self._layout = layout
        self._warmup_url = warmup_url
        self._opener_factory = opener_factory or _legacy_opener

    def url_for(self, on: date) -> str:
        return self.url_template.format(d=on, mon=_LEGACY_MONTH_ABBREVIATIONS[on.month - 1])

    def fetch(self, on: date) -> bytes:
        legacy_text = fetch_legacy_archive_text(
            f"{self.exchange} (legacy archive)",
            self.url_for(on),
            on,
            self._warmup_url,
            self._opener_factory,
        )
        return _to_udiff_csv(legacy_text, self._layout)


def fetch_legacy_archive_text(
    label: str, url: str, on: date, warmup_url: str | None = None, opener_factory=None
) -> str:
    """Fetch one zipped pre-UDiFF archive file and return its CSV text.

    Shared by the cash-market legacy tiers above and the NSE F&O legacy tier
    (``app.prices.fno_sources``), so the warm-up cookie dance and the 404 / non-zip
    "nothing published" rules exist once.
    """
    opener = (opener_factory or _legacy_opener)()
    try:
        if warmup_url:
            warmup = urllib.request.Request(warmup_url, headers=LEGACY_BROWSER_HEADERS)
            opener.open(warmup, timeout=TIMEOUT_SECONDS).close()

        request = urllib.request.Request(url, headers=LEGACY_BROWSER_HEADERS)
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotPublished(f"{label}: nothing published for {on}") from exc
        raise FetchFailed(f"{label}: HTTP {exc.code} for {on}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchFailed(f"{label}: {type(exc).__name__} for {on}") from exc

    if body[:2] != b"PK":
        # Neither archive 404s a date it does not have: BSE answers HTTP 200 with its
        # own HTML page, which would otherwise parse as zero rows and be reported as a
        # successful fetch of an empty market. A real archive is always a zip.
        raise NotPublished(f"{label}: nothing published for {on} (non-zip response)")
    archive = zipfile.ZipFile(io.BytesIO(body))
    return archive.read(archive.namelist()[0]).decode("utf-8", "replace")


def legacy_nse_source(opener_factory=None) -> LegacyBhavcopySource:
    return LegacyBhavcopySource(
        "NSE", LEGACY_NSE_URL, NSE_LEGACY_LAYOUT, LEGACY_NSE_WARMUP_URL, opener_factory
    )


def legacy_bse_source(opener_factory=None) -> LegacyBhavcopySource:
    return LegacyBhavcopySource(
        "BSE", LEGACY_BSE_URL, BSE_LEGACY_LAYOUT, None, opener_factory
    )


class TieredBhavcopySource:
    """Current format on/after CURRENT_FORMAT_SINCE, the legacy archive before it.

    One ``BhavcopySource`` per exchange, so ``fetch_day`` and every other caller stays
    unaware that two URL shapes and two CSV layouts sit behind it; ``fetch`` returns
    UDiFF-shaped bytes either way, and ``app.prices.udiff.parse`` remains the only
    bhavcopy parser in the system.

    Dispatched by DATE rather than by trying one and falling back, because "no file"
    is indistinguishable from "exchange holiday" on both archives -- a fallback would
    spend a second request on every weekend forever and still learn nothing. Both
    exchanges happen to share the same boundary, and the formats OVERLAP either side of
    it (BSE still published its legacy file into mid-2024), so the constant is not
    knife-edge and moving it is safe.
    """

    def __init__(
        self,
        exchange: str,
        current: BhavcopySource,
        legacy: BhavcopySource,
        since: date = NSE_CURRENT_FORMAT_SINCE,
    ):
        self.exchange = exchange
        self._current = current
        self._legacy = legacy
        self._since = since

    def fetch(self, on: date) -> bytes:
        if on < self._since:
            return self._legacy.fetch(on)
        return self._current.fetch(on)


def nse_source(opener=None) -> TieredBhavcopySource:
    return TieredBhavcopySource(
        "NSE", HttpBhavcopySource("NSE", NSE_URL, opener), legacy_nse_source()
    )


def bse_source(opener=None) -> TieredBhavcopySource:
    return TieredBhavcopySource(
        "BSE", HttpBhavcopySource("BSE", BSE_URL, opener), legacy_bse_source()
    )
