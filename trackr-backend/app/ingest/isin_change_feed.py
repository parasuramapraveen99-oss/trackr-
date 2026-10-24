"""Where automated ISIN-change evidence comes from -- NSE's own circulars API.

INTERIM SOURCE, same posture as ``app.ingest.corporate_action_feed`` and for the same
reason: there is no licensed feed behind this yet, and NSE's interactive site API is not
a published archive with a stability guarantee. Confirmed live, 2026-09-20:
``www.nseindia.com/api/circulars?fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY`` returns JSON
listing every circular in range, each with a free-text ``sub`` (subject) field; filtering
that for ``"change in isin"`` finds NSE's "Change in ISIN" notices (measured: 58 in 2022,
56 in 2023, 73 in 2024, 59 in 2025). The circular's own PDF, at the ``circFilelink`` the
list response gives, is fetched next and parsed -- confirmed against three real circulars
(Tata Steel, July 2022; Oriana Power and Le Merite Exports, 2026) that the body is a
stable one-row table: ``Symbol | Company Name | New ISIN | Remarks``.

WHY THIS MODULE NEVER WRITES OR TOUCHES A DATABASE. Same split as
``app.ingest.corporate_action_feed``/``app.db.corporate_actions.refresh_corporate_action_feed``
and ``app.ingest.corporate_action_feed.parse_dividend_feed``/
``app.db.dividend_events.refresh_dividend_feed``: this module fetches and parses only.
``app.db.isin_change_circulars.refresh_isin_change_feed`` is what holds a connection,
resolves the symbol join against ``security``, calls ``app.db.security_alias.add_alias``,
and owns the circuit-breaker state in ``feed_poll_state``.

WHY THE OLD ISIN IS NEVER IN THIS MODULE'S OUTPUT. Every circular checked states the
SYMBOL and the NEW isin -- never the one being retired. "Which ISIN does this retire" is
an inference this codebase makes by joining the symbol against its own ``security``
table, and that join belongs at the DB layer, not here: this module has no database
access and must not guess an identity from text alone. See ``ParsedCircular`` below.

RELIABILITY, because this is confirmed to be an unofficial, undocumented endpoint with no
stability guarantee -- NSE's Terms of Use restrict automated collection from the
interactive site the same way ``corporate_action_feed``'s docstring already notes:

  - a token-bucket throttle caps this module's own request rate at 3/sec, shared across
    the homepage warm-up, the circulars-list call, and every matched PDF fetch in one
    poll run;
  - exponential backoff (1s/2s/4s/8s) retries a TRANSIENT failure (5xx, timeout,
    connection reset) up to 4 times before giving up on that one request;
  - a non-transient failure (4xx, including a 403 from the anti-bot layer) is NOT
    retried -- hammering an already-blocking response with the same request wastes
    budget and cannot self-resolve the way a timeout can.

The circuit breaker itself (skip an entire poll after N consecutive failed runs) needs
state that survives between weekly cron invocations, so it lives in ``feed_poll_state``
and is checked/updated by ``refresh_isin_change_feed`` -- this module's fetchers
(``CircularListSource.list_circulars``/``fetch_pdf``) have no memory of past runs and
always attempt the request they're asked to make.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from app.exchange_reference import InvalidExchangeReference, MissingExchangeReference, needs_warmup
from app.ingest.nse_session import cookie_session
from app.prices.sources import BROWSER_HEADERS, TIMEOUT_SECONDS

NSE_HOMEPAGE_URL = "https://www.nseindia.com/"

#: Confirmed live, 2026-09-20: camelCase ``fromDate``/``toDate`` returns the full range;
#: the snake_case names silently ignore both and return only a short rolling window.
NSE_CIRCULARS_URL = (
    "https://www.nseindia.com/api/circulars?fromDate={start:%d-%m-%Y}&toDate={end:%d-%m-%Y}"
)
NSE_CIRCULARS_REFERER = "https://www.nseindia.com/resources/exchange-communication-circulars"

#: Subject-line filter for the circulars list. Case-insensitive; the confirmed live
#: examples are all "Change in ISIN -- <Company> (<SYMBOL>)".
_ISIN_CHANGE_SUBJECT_RE = re.compile(r"change in isin", re.IGNORECASE)

#: Requests per second across one poll run -- the homepage warm-up, the circulars-list
#: call, and every matched circular's PDF fetch all draw from the same bucket.
_MAX_REQUESTS_PER_SECOND = 3

#: Transient-failure backoff: up to this many retries (5 attempts total: the first try
#: plus these 4), with the delay growing 1s -> 2s -> 4s -> 8s between them.
_MAX_RETRIES = 4
_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)


class IsinChangeFeedFetchFailed(Exception):
    """The request should have worked and did not, even after retrying a transient
    failure -- network error, non-200, or the anti-bot layer blocked it. Caught by
    ``refresh_isin_change_feed``, which counts it toward the circuit breaker rather than
    letting it propagate; never raised out of a poll entry point."""


class IsinChangeCircularFormatError(Exception):
    """A circular's PDF text does not contain the fields every confirmed live example
    has (Symbol / New ISIN / Remarks / the two dates) -- the clearest signal NSE
    reshaped the notice, not a routine parse miss. One malformed circular's format
    error must not abort a poll processing others; see ``refresh_isin_change_feed``."""


@runtime_checkable
class CircularListSource(Protocol):
    def list_circulars(self, start: date, end: date) -> bytes: ...
    def fetch_pdf(self, url: str) -> bytes: ...


class _TokenBucket:
    """A minimal rate limiter: block until issuing another token would not exceed
    ``rate`` per second. Not a general-purpose limiter -- just enough to keep this
    module's own request volume, all of it inside one poll run, under the stated cap.

    ``sleep`` and ``now`` are injectable so tests can run thousands of simulated
    requests with no real delay -- the default ``time.sleep``/``time.monotonic`` pairing
    is what actually paces a live poll.
    """

    def __init__(
        self,
        rate: float,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = 1.0 / rate
        self._sleep = sleep
        self._now = now
        self._last_issued: float | None = None

    def wait(self) -> None:
        if self._last_issued is not None:
            elapsed = self._now() - self._last_issued
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_issued = self._now()


def _is_transient(exc: BaseException) -> bool:
    """5xx, timeout, or a connection-level error -- the class of failure a retry can
    plausibly resolve. A 4xx (403 from the anti-bot layer, 404, etc.) is NOT transient:
    the same request will get the same answer, and retrying it four times only spends
    budget against an endpoint already confirmed to rate-limit aggressively."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


class NseCircularListSource:
    """INTERIM implementation -- see module docstring. Replace this class, not its
    caller, when a licensed feed stands behind ISIN-change evidence.

    All network calls in this class -- and only these -- go through ``self._bucket`` and
    ``self._retrying``, so a poll's total request volume (warm-up + list + N PDFs) is
    rate-limited and retried uniformly regardless of which method issued the request.
    """

    def __init__(
        self,
        opener: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: int = TIMEOUT_SECONDS,
    ) -> None:
        self._opener = opener if opener is not None else cookie_session().open
        self._bucket = _TokenBucket(_MAX_REQUESTS_PER_SECOND, sleep=sleep)
        self._sleep = sleep
        self._timeout = timeout

    def _retrying(self, make_request: Callable[[], bytes], description: str) -> bytes:
        last_exc: BaseException | None = None
        for attempt in range(_MAX_RETRIES + 1):
            self._bucket.wait()
            try:
                return make_request()
            except Exception as exc:  # noqa: BLE001 -- reclassified immediately below
                if isinstance(exc, (MissingExchangeReference, InvalidExchangeReference)):
                    raise
                if not _is_transient(exc) or attempt == _MAX_RETRIES:
                    raise IsinChangeFeedFetchFailed(
                        f"{description}: {type(exc).__name__}"
                    ) from exc
                last_exc = exc
                self._sleep(_BACKOFF_SECONDS[attempt])
        # Unreachable: the loop above always returns or raises. Kept for type-checkers
        # and to make the invariant explicit rather than implicit in the loop shape.
        raise IsinChangeFeedFetchFailed(f"{description}: exhausted retries") from last_exc

    def list_circulars(self, start: date, end: date) -> bytes:
        def _warmup() -> bytes:
            warmup = urllib.request.Request(NSE_HOMEPAGE_URL, headers=BROWSER_HEADERS)
            self._opener(warmup, timeout=self._timeout).close()
            return b""

        if needs_warmup(self._opener):
            self._retrying(_warmup, "NSE session warm-up")

        def _list() -> bytes:
            request = urllib.request.Request(
                NSE_CIRCULARS_URL.format(start=start, end=end),
                headers={
                    **BROWSER_HEADERS,
                    "Referer": NSE_CIRCULARS_REFERER,
                    "Accept": "application/json",
                },
            )
            with self._opener(request, timeout=self._timeout) as response:
                return response.read()

        return self._retrying(_list, "NSE circulars list")

    def fetch_pdf(self, url: str) -> bytes:
        parsed_url = urllib.parse.urlsplit(url)
        if (parsed_url.scheme != "https" or parsed_url.hostname not in {
            "nsearchives.nseindia.com", "archives.nseindia.com", "www.nseindia.com",
        } or parsed_url.username or parsed_url.password or parsed_url.port not in (None, 443)):
            raise IsinChangeFeedFetchFailed("Circular URL is not an HTTPS NSE source")
        def _fetch() -> bytes:
            request = urllib.request.Request(
                url, headers={**BROWSER_HEADERS, "Referer": NSE_HOMEPAGE_URL}
            )
            with self._opener(request, timeout=self._timeout) as response:
                payload = response.read(12 * 1024 * 1024 + 1)
            if len(payload) > 12 * 1024 * 1024:
                raise IsinChangeFeedFetchFailed("Circular PDF exceeds size limit")
            return payload

        return self._retrying(_fetch, f"NSE circular PDF {url}")


def nse_circular_list_feed(
    opener: Any = None, sleep: Callable[[float], None] = time.sleep
) -> NseCircularListSource:
    return NseCircularListSource(opener, sleep=sleep)


@dataclass(frozen=True, slots=True)
class CircularListing:
    """One row from the circulars list endpoint that looks like an ISIN change --
    filtered on subject only. Not yet fetched or parsed; ``circular_number`` is what
    ``refresh_isin_change_feed`` dedupes against ``isin_change_circular``."""

    circular_number: str
    circular_ref: str
    circular_date: date
    subject: str
    pdf_url: str


@dataclass(frozen=True, slots=True)
class ParsedCircular:
    """One ISIN-change circular's PDF, structured. Deliberately carries no ``old_isin``
    -- see the module docstring's "why the old ISIN is never in this module's output".
    ``reason_class``/``reason_confidence`` are ALWAYS ``None`` today. They existed for a
    model-backed classifier of the Remarks text that has since been removed from this
    codebase; the ``isin_change_circular`` columns behind them (migration 031) are kept
    because they are nullable, purely a review-queue enrichment, and nothing downstream
    has ever branched on them. The fields stay so a future classifier has somewhere to
    land without a migration."""

    symbol: str
    new_isin: str
    remarks_raw: str
    wef_date: date
    ex_date: date
    reason_class: str | None = None
    reason_confidence: Any = None


def _required(pattern: str, text: str, field: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        raise IsinChangeCircularFormatError(f"circular is missing {field}")
    return match.group(1).strip()


#: The "Sub:" line is a far more reliable source for the symbol than the table row --
#: confirmed live across all three circulars checked, always "Sub: Change in ISIN --
#: <Company Name> (<SYMBOL>)" on one line, with no wrap. The table row, by contrast, is
#: NOT reliably one line: see ``_extract_remarks`` below for the wrap this parser has to
#: undo. NSE's dash character in this line is an en-dash in every sample; a plain hyphen
#: is accepted too in case a future circular uses one.
#: The symbol is the LAST bracketed group on that line (greedy ``[^\n]*``), and is matched
#: case-SENSITIVELY via ``(?-i:...)`` even though ``_required`` searches with IGNORECASE:
#: real NSE/CML/72189 reads "SKM Egg Products Export (India) Limited (SKMEGGPROD)", and the
#: earlier first-bracket, case-blind pattern stored "India" as the symbol.
_SUBJECT_SYMBOL_RE = r"Sub:\s*Change in ISIN\s*[-–—][^\n]*\((?-i:([A-Z0-9&]+))\)"
_NEW_ISIN_RE = r"\b(IN[A-Z0-9]{10})\b"
_WEF_DATE_RE = r"changed\s+w\.e\.f\.\s+([A-Za-z]+ \d{1,2},? \d{4})"
_EX_DATE_RE = r"[Ee]x-?[Dd]ate[^,]*i\.e\.,?\s+([A-Za-z]+ \d{1,2},? \d{4})"

#: Bounds the search for the table block: everything between the "shall be changed
#: w.e.f. ..." sentence and "The new ISIN shall be effective ...". Confirmed present,
#: unbroken across a line, in all three circulars checked.
_TABLE_START_RE = re.compile(r"changed\s+w\.e\.f\.[^\n]*\n", re.IGNORECASE)
_TABLE_END_RE = re.compile(r"The new ISIN shall be effective", re.IGNORECASE)

#: The four column names, tolerant of the header appearing as one line (Tata Steel) or
#: split across several by a PDF's internal layout (Oriana Power) -- matched by content,
#: not position, rather than assuming a fixed wrap point.
_HEADER_TOKENS = ("symbol", "company name", "new isin", "remarks")
_HEADER_ONLY_LINE_RE = re.compile(r"^sr\.?\s*no\.?$|^sr\.?$|^no\.?$", re.IGNORECASE)


def _parse_circular_date(raw: str) -> date:
    cleaned = raw.replace(",", "")
    # Full month names in current circulars; 2022-23 circulars abbreviate ("Jan 06, 2023").
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise IsinChangeCircularFormatError(f"unrecognised date {raw!r}")


def _is_header_line(line: str) -> bool:
    lowered = line.lower()
    if _HEADER_ONLY_LINE_RE.match(lowered):
        return True
    return all(token in lowered for token in _HEADER_TOKENS)


def _extract_remarks(pdf_text: str, new_isin: str) -> str:
    """Reconstruct the Remarks cell from the table block.

    NOT a single-line extraction. Confirmed live that a Remarks cell wrapping onto two
    lines (Oriana Power, 2026) reorders relative to plain reading order: the first
    wrapped line of the cell's text is emitted BEFORE the data row, and the rest after
    it -- e.g. the raw block reads ``"Sub-Division from Rs. 10/- to\\n1 ORIANA ...
    INE0OUT01027\\nRs. 2/-"``, not the visually-intact sentence. A parser assuming the
    remarks sit after the ISIN on one line (true for Tata Steel and Le Merite, both
    short enough to fit one line) silently drops the leading fragment on any circular
    whose remarks wrap. This reassembles the sentence regardless of which shape the PDF
    produced: strip the header lines, then the row containing ``new_isin`` is the
    anchor -- everything strictly before it, the text after the ISIN on its own line,
    and everything strictly after it, joined in that order, is the full Remarks text.
    """
    start = _TABLE_START_RE.search(pdf_text)
    end = _TABLE_END_RE.search(pdf_text)
    if start is None or end is None or start.end() > end.start():
        raise IsinChangeCircularFormatError("circular is missing the Symbol/New ISIN table")

    lines = [line.strip() for line in pdf_text[start.end() : end.start()].splitlines()]
    lines = [line for line in lines if line and not _is_header_line(line)]

    isin_row = next((i for i, line in enumerate(lines) if new_isin in line), None)
    if isin_row is None:
        raise IsinChangeCircularFormatError("circular's New ISIN row not found in the table")

    after_isin = lines[isin_row].split(new_isin, 1)[1].strip()
    fragments = lines[:isin_row] + ([after_isin] if after_isin else []) + lines[isin_row + 1 :]
    remarks = re.sub(r"\s+", " ", " ".join(fragments)).strip()
    if not remarks:
        raise IsinChangeCircularFormatError("circular is missing Remarks")
    return remarks


def parse_isin_change_circular(pdf_text: str) -> ParsedCircular:
    """One circular's extracted text into structured fields. Raises
    ``IsinChangeCircularFormatError`` -- never a bare exception -- when a required
    field is absent, which is the signal a format change happened, not a row to quietly
    skip. Confirmed against three live circulars, including the Remarks-wrap case; see
    the module docstring and ``_extract_remarks``."""
    # WORDED FOR THE HUMAN WHO SCANS THE QUEUE, not for the parser. This is the guard a
    # non-equity ISIN-change circular -- an NCD, debenture or bond series -- fails, every
    # time and by design: the "Sub:" line's trailing parenthesis holds an equity trading
    # SYMBOL, and a debt instrument has none to put there. The old message ("circular is
    # missing the symbol in the Sub: line") described the regex rather than the situation,
    # so a perfectly correct skip read like a bug to anyone reviewing the queue later.
    #
    # It stays a parse_failed and stays in the queue, and is worded "typically" rather
    # than "is", because the SAME absence is also what a genuine format change on a real
    # equity circular would look like. Reclassifying this as benign on the strength of a
    # guess about which one it was is the reclassification that would eventually swallow a
    # real ISIN change. Naming the likely cause is a hint to the reviewer; it is not a
    # decision, and nothing downstream branches on it.
    symbol = _required(
        _SUBJECT_SYMBOL_RE,
        pdf_text,
        'an equity trading symbol in its "Sub:" line -- typically a non-equity '
        "(NCD/debenture/bond) ISIN change, which this equity-only system skips by design; "
        "review it only if the circular does concern a listed equity, in which case the "
        "circular format has changed",
    )
    new_isin = _required(_NEW_ISIN_RE, pdf_text, "a New ISIN")
    wef_raw = _required(_WEF_DATE_RE, pdf_text, "the w.e.f. date")
    ex_raw = _required(_EX_DATE_RE, pdf_text, "the Ex-date")
    remarks = _extract_remarks(pdf_text, new_isin)

    return ParsedCircular(
        symbol=symbol,
        new_isin=new_isin,
        remarks_raw=remarks,
        wef_date=_parse_circular_date(wef_raw),
        ex_date=_parse_circular_date(ex_raw),
    )


def parse_circular_listing(payload: bytes) -> list[CircularListing]:
    """The circulars-list JSON payload, filtered to rows whose subject reads as an
    ISIN change. Everything else in the payload (dividends, surveillance notices,
    compliance filings) is silently not a match here -- this is a routine filter, not a
    format assertion, so an empty result is not ``IsinChangeCircularFormatError``."""
    import json

    try:
        data = json.loads(payload)
        rows = data["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise IsinChangeCircularFormatError(f"circulars list: {exc}") from exc

    listings: list[CircularListing] = []
    for row in rows:
        subject = row.get("sub", "")
        if not _ISIN_CHANGE_SUBJECT_RE.search(subject):
            continue
        try:
            listings.append(
                CircularListing(
                    circular_number=str(row["circNumber"]),
                    circular_ref=row["circDisplayNo"],
                    circular_date=datetime.strptime(row["cirDate"], "%Y%m%d").date(),
                    subject=subject,
                    pdf_url=row["circFilelink"],
                )
            )
        except (KeyError, ValueError) as exc:
            raise IsinChangeCircularFormatError(
                f"circulars list: ISIN-change row missing a field: {exc}"
            ) from exc
    return listings


class IsinChangePdfExtractionError(Exception):
    """pdfplumber is missing or could not read the PDF bytes at all -- distinct from
    ``IsinChangeCircularFormatError``, which means extraction worked but the expected
    fields are not in the text."""


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """The one place this module touches a PDF library, so a parsing-strategy change
    never ripples into ``parse_isin_change_circular``, which only ever sees text. Same
    library and the same lazy-import-with-install-hint pattern as
    ``app.ingest.cas_pdf.extract_text``, for one PDF dependency in this codebase rather
    than two."""
    import io

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise IsinChangePdfExtractionError(
            "pdfplumber is not installed. pip install -e '.[pdf]'"
        ) from exc

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:  # noqa: BLE001 - pdfplumber raises assorted types
        raise IsinChangePdfExtractionError(f"could not read circular PDF: {exc}") from exc
