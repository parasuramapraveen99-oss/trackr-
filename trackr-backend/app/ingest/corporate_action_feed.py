"""Where automated corporate-action notices come from -- NSE's own corporate-filings API.

INTERIM SOURCE, NOT THE PERMANENT ONE -- stated once here, loudly, because this is the
one thing about this module most likely to need replacing. There is no static, published
archive file for corporate actions the way ``app.prices.sources`` has for bhavcopy
(confirmed: no ``nsearchives.nseindia.com`` equivalent exists). What works is NSE's own
interactive site API (``www.nseindia.com/api/corporates-corporateActions``), which needs
a bootstrapped session cookie and a Referer header to get past its anti-bot layer --
confirmed live, 2026-09-10. That is a materially different risk/reliability profile from
bhavcopy's own published archive: NSE's Terms of Use restrict automated collection from
the interactive site specifically. Kept behind ``CorporateActionFeedSource`` (a
``Protocol``, same shape as ``app.prices.sources.BhavcopySource``) for exactly one
reason: so a licensed vendor (e.g. Accord Fintech, a confirmed authorised NSE/BSE
reseller) can replace ``NseCorporateActionFeedSource`` later without
``app.db.corporate_actions.refresh_corporate_action_feed`` or anything downstream of it
changing at all.

WHY THIS MODULE NEVER EMITS A VERIFIED ``CorporateAction``. Parsing NSE's free-text
``subject`` field ("Bonus 1:1", "Dividend - Rs 1.50 Per Share", "Sub-Division ...") is a
best-effort regex classification, not a read of a structured, guaranteed-correct field --
see ``docs/ENGINE_SPEC.md``'s "No ratio strings" note on why this codebase never trusts
notation like this at face value. This module's job stops at producing a structured,
UNVERIFIED candidate; ``app.db.corporate_actions.refresh_corporate_action_feed`` is what
runs the actual trust guards (``app.engine.corporate_actions.screen``, in particular
``check_price_consistency`` -- the real, independent-of-this-parser corroboration) before
anything is ever marked ``VERIFIED``.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import permutations
from typing import Any, Protocol, runtime_checkable

from app.ai.jev import JevResponseError, JevUnavailable
from app.engine.corporate_actions import CorpActionType
from app.exchange_reference import needs_warmup
from app.ingest.action_source import ActionSourceError
from app.ingest.nse_session import cookie_session
from app.prices.sources import BROWSER_HEADERS, TIMEOUT_SECONDS

#: NSE's interactive corporate-filings page -- confirmed live, 2026-09-10, that the
#: underlying API 403s without a warmed session cookie AND this exact Referer.
NSE_HOMEPAGE_URL = "https://www.nseindia.com/"
NSE_CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}"
)
NSE_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-actions"

#: Observation parser version for rows of this feed (``corporate_action_observation``).
#: Bump it when ``classify_subject`` or the row parsers change meaning.
NSE_PARSER_VERSION = "nse-corporate-actions-v1"


class CorporateActionFeedFetchFailed(ActionSourceError):
    """The request should have worked and did not -- network error, non-200, or the
    anti-bot layer blocked it even after a session warm-up. Alerts."""


class CorporateActionFeedFormatError(ActionSourceError):
    """The response parsed as JSON but is missing a field every row of this feed has
    carried so far -- the clearest signal NSE reshaped the API under us. A row this
    parser cannot classify at all (see ``parse_corporate_action_feed``) is NOT this --
    that is the expected, routine case of a notice type this parser doesn't yet cover,
    handled by simply not producing a candidate for it."""


@runtime_checkable
class CorporateActionFeedSource(Protocol):
    def fetch(self, start: date, end: date) -> bytes: ...


class NseCorporateActionFeedSource:
    """INTERIM implementation -- see module docstring. Replace this class, not its
    caller, when a licensed feed is in place."""

    def __init__(self, opener: Any = None):
        self._opener = opener if opener is not None else cookie_session().open

    def fetch(self, start: date, end: date) -> bytes:
        # Both calls use the same private cookie jar. Plain urlopen does not retain
        # the warm-up response's Set-Cookie headers for the API request.
        try:
            warmup = urllib.request.Request(NSE_HOMEPAGE_URL, headers=BROWSER_HEADERS)
            if needs_warmup(self._opener):
                self._opener(warmup, timeout=TIMEOUT_SECONDS).close()

            request = urllib.request.Request(
                NSE_CORP_ACTIONS_URL.format(start=start, end=end),
                headers={**BROWSER_HEADERS, "Referer": NSE_REFERER, "Accept": "application/json"},
            )
            with self._opener(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise CorporateActionFeedFetchFailed(f"NSE corporate actions: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CorporateActionFeedFetchFailed(
                f"NSE corporate actions: {type(exc).__name__}"
            ) from exc


def nse_corporate_action_feed(opener: Any = None) -> NseCorporateActionFeedSource:
    return NseCorporateActionFeedSource(opener)


@dataclass(frozen=True, slots=True)
class FeedRow:
    """One notice, structured but UNVERIFIED -- the boundary this module stops at.

    ``price_cum``/``price_ex`` are never populated here: NSE's corporate-actions feed
    carries no price at all. ``app.db.corporate_actions.refresh_corporate_action_feed``
    looks those up from ``app.db.prices.prices_on_date`` before this can ever become a
    ``CorporateAction``.
    """

    isin: str
    symbol: str
    action_type: CorpActionType
    ex_date: date
    bonus_new: int | None = None
    bonus_for: int | None = None
    face_value_before: Decimal | None = None
    face_value_after: Decimal | None = None
    raw_subject: str = ""
    #: ``recDate`` when NSE sent a usable one. Evidence for a BONUS lot's provisional tax
    #: acquisition date (ENGINE_SPEC section 3); never moves the ex-date or quantity.
    record_date: date | None = None
    #: The exact NSE row this was parsed from, so the caller can link the row's
    #: ``corporate_action_observation`` (keyed on these bytes) to the action it produced.
    raw: dict[str, Any] | None = field(default=None, compare=False, repr=False)


_REQUIRED_FIELDS = {"isin", "symbol", "exDate", "subject"}

#: "Bonus 1:1", "Bonus Issue 2:1", "Bonus  1 : 3" -- whitespace-tolerant, case-insensitive.
_BONUS_RE = re.compile(r"bonus\D*?(\d+)\s*:\s*(\d+)", re.IGNORECASE)

#: "Sub-Division (Split) - From Rs.10/- To Rs.2/-" and equivalent phrasings NSE uses.
#: "Rs" vs "Re" is not a typo to normalise away: Indian English grammar uses "Re." only
#: for exactly the singular unit (Re. 1/-), "Rs." for everything else (Rs. 2/-, Rs.
#: 10/-) -- confirmed live, a real and common phrasing for splits down to face value 1
#: (7 of 15 real split/bonus notices in one three-month pull), silently missed entirely
#: before this alternation was added. A false negative here is safe (the notice simply
#: never becomes a candidate, never a wrong one) but was defeating the point of
#: automating the common case.
_SPLIT_RE = re.compile(
    r"(?:sub[- ]division|stock\s*split).*?rs?e?\.?\s*(\d+(?:\.\d+)?)\D+rs?e?\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

#: A real, confirmed false positive from live data: "Scheme Of Arrangement - Bonus
#: Ncrps 46:1" matches ``_BONUS_RE`` on its ratio but is NOT an equity bonus -- NCRPS is
#: Non-Convertible Redeemable Preference Shares, a different instrument entirely, issued
#: under a scheme of arrangement, not a plain bonus issue. Applying its ratio to this
#: ISIN's equity quantity would be a real, quantified wrong number, not a theoretical
#: risk. Any subject containing one of these is treated as unclassifiable -- consistent
#: with the "no candidate beats a wrong candidate" rule above -- rather than trusting a
#: bare ratio match. Not exhaustive; extend this list the next time a live pull surfaces
#: another qualifier that changes what the ratio actually means.
_DISQUALIFYING_TERMS = (
    "scheme of arrangement",
    "ncrps",
    "preference share",
    "debenture",
    "warrant",
    "partly paid",
    "demerger",
    "merger",
    "amalgamation",
    # Confirmed live in TWO independent 3-year pulls (2020-2022: "Capital Reduction
    # Pursuant To Nclt Order"; 2023-2025: "Capital Reduction"), found auditing this list
    # for demerger/merger phrasing coverage. Neither occurrence was ratio-shaped, so this
    # was not yet a live miscost -- but a capital reduction changes share count/value by
    # a mechanism a plain bonus/split ratio must never be asked to represent, exactly the
    # "qualifier that changes what the ratio means" this list exists to name. "Redemption"
    # (also seen in that audit, 2 occurrences) was NOT added: it names an instrument-level
    # event (debenture/warrant redemption, already covered by those terms when paired with
    # one) rather than a company-level restructuring, and neither occurrence was
    # ratio-shaped either -- added on a repeat sighting, not preemptively.
    "reduction of capital",
    "capital reduction",
)


def is_plain_notice(subject: str) -> bool:
    lowered = subject.lower()
    return not any(term in lowered for term in _DISQUALIFYING_TERMS)


def parse_nse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, "%d-%b-%Y").date()
    except (ValueError, TypeError):
        return None


#: classify_subject's restructuring classes, first match wins. Demerger precedes merger
#: because one word contains the other. Each term is also in _DISQUALIFYING_TERMS.
_RESTRUCTURING_CLASSES = (
    ("DEMERGER", ("demerger",)),
    ("MERGER", ("merger", "amalgamation", "scheme of arrangement")),
    ("CAPITAL_REDUCTION", ("reduction of capital", "capital reduction")),
)

#: Notice classes no parser here reads. Matched only on a plain notice, after the
#: bonus/split/dividend shapes, so a classification never disagrees with what the row
#: parsers produce from the same subject.
_REVIEW_CLASSES = (
    ("BUYBACK", re.compile(r"buy[\s-]?back", re.IGNORECASE)),
    ("CONSOLIDATION", re.compile(r"consolidation", re.IGNORECASE)),
    ("RIGHTS", re.compile(r"\brights?\b", re.IGNORECASE)),
)


def classify_subject(subject: str) -> str:
    """Name the kind of notice a subject announces. DETECTION ONLY (invariant 3): the
    result is recorded on an observation and never authorizes a write.

    Returns one of BONUS, SPLIT, DIVIDEND, RIGHTS, BUYBACK, MERGER, DEMERGER,
    CAPITAL_REDUCTION, CONSOLIDATION or UNKNOWN. A subject with an instrument qualifier
    from _DISQUALIFYING_TERMS (NCRPS, debentures, warrants, ...) and no restructuring term
    is UNKNOWN: it is about something other than the plain equity.
    """
    lowered = subject.lower()
    if not is_plain_notice(subject):
        for name, terms in _RESTRUCTURING_CLASSES:
            if any(term in lowered for term in terms):
                return name
        return "UNKNOWN"
    if _BONUS_RE.search(subject):
        return "BONUS"
    if _SPLIT_RE.search(subject):
        return "SPLIT"
    if _DIVIDEND_RE.search(subject):
        return "DIVIDEND"
    for name, pattern in _REVIEW_CLASSES:
        if pattern.search(subject):
            return name
    return "UNKNOWN"


def parse_corporate_action_feed(payload: bytes) -> list[FeedRow]:
    """NSE's ``corporates-corporateActions`` JSON array into structured, unverified rows.

    A notice type this parser doesn't recognise (dividends, rights, mergers, anything
    whose ``subject`` text doesn't match ``_BONUS_RE``/``_SPLIT_RE``) is simply NOT
    included in the result -- not an error. This parser's only job is "classify what it
    can classify with confidence"; ``docs/ENGINE_SPEC.md``'s "No ratio strings" rule
    means a low-confidence guess at an ambiguous notice is worse than no candidate at
    all, since ``refresh_corporate_action_feed`` has no way to distinguish "this parser
    guessed wrong" from "this parser correctly read a real 4:1 bonus".

    Raises:
        CorporateActionFeedFormatError: the payload isn't a JSON array of objects, or an
            object is missing a field every row has carried so far (``isin``, ``symbol``,
            ``exDate``, ``subject``) -- the shape NSE has always sent, not a value this
            parser is choosy about.
    """
    import json

    try:
        records = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorporateActionFeedFormatError(f"not valid JSON: {exc}") from exc
    if not isinstance(records, list):
        raise CorporateActionFeedFormatError(f"expected a JSON array, got {type(records).__name__}")

    out: list[FeedRow] = []
    for i, record in enumerate(records):
        if not isinstance(record, dict) or not _REQUIRED_FIELDS <= set(record):
            raise CorporateActionFeedFormatError(
                f"row {i}: missing required field(s) {sorted(_REQUIRED_FIELDS - set(record))}"
            )
        isin = str(record["isin"]).strip()
        symbol = str(record["symbol"]).strip()
        subject = str(record["subject"]).strip()
        if not isin or not subject:
            continue  # blank rows are a known artefact of this feed, not a format break
        ex_date = parse_nse_date(str(record["exDate"]).strip())
        if ex_date is None:
            continue  # "-" is a real, routine value NSE sends for a not-yet-fixed date

        if not is_plain_notice(subject):
            continue  # a qualifier changes what the ratio means -- see _DISQUALIFYING_TERMS

        record_date = parse_nse_date(str(record.get("recDate", "")).strip())
        if record_date is not None and record_date < ex_date:
            # Cannot be true; in practice transposed fields. Drop the date, keep the
            # notice: a missing record date only makes a bonus lot's date provisional.
            record_date = None

        bonus_match = _BONUS_RE.search(subject)
        if bonus_match:
            out.append(
                FeedRow(
                    isin=isin, symbol=symbol, action_type=CorpActionType.BONUS, ex_date=ex_date,
                    bonus_new=int(bonus_match.group(1)), bonus_for=int(bonus_match.group(2)),
                    raw_subject=subject, record_date=record_date, raw=record,
                )
            )
            continue

        split_match = _SPLIT_RE.search(subject)
        if split_match:
            try:
                before, after = Decimal(split_match.group(1)), Decimal(split_match.group(2))
            except InvalidOperation:
                continue
            out.append(
                FeedRow(
                    isin=isin, symbol=symbol, action_type=CorpActionType.SPLIT, ex_date=ex_date,
                    face_value_before=before, face_value_after=after, raw_subject=subject,
                    record_date=record_date, raw=record,
                )
            )
            continue

        # Anything else (dividend, rights, merger, demerger, buyback, AGM/EGM notices,
        # ...) is out of this parser's scope by design -- see the docstring above.

    return out


# ===========================================================================
# DIVIDEND NOTICES -- the same payload, a separate parser, a separate row type.
#
# WHY A SEPARATE PARSE FUNCTION rather than another branch inside
# ``parse_corporate_action_feed``. A dividend is not a ``CorpActionType`` and must never
# become one: see migration 029's header for the full argument, the short version being
# that ``check_price_consistency`` passes vacuously for any non-BONUS/SPLIT type, so a
# DIVIDEND member of that enum would inherit ``apply_known_actions_for_account``'s
# auto-apply path with no real guard behind it. Keeping the row types apart means the two
# kinds of notice cannot be confused downstream by a missing ``if``, and the existing
# parser's behaviour is unchanged -- it still drops dividends, which is now correct rather
# than merely incomplete, because this function is what reads them.
#
# WHAT THIS PARSER REFUSES TO DO, and why each refusal is safer than the alternative. Same
# governing rule as the bonus/split parser: no candidate beats a wrong candidate, because
# nothing downstream can tell "this parser guessed" from "this parser read a real notice".
# ===========================================================================


@dataclass(frozen=True, slots=True)
class DividendFeedRow:
    """One dividend notice, structured but UNVERIFIED.

    Carries no quantity and no total: multiplying by a holder's position is what turns a
    declaration into an expectation, and that happens on read, never here.
    """

    isin: str
    symbol: str
    ex_date: date
    amount_per_share: Decimal
    kind: str  # a DividendKind value; kept as str so this module imports no engine enum
    record_date: date | None = None
    raw_subject: str = ""


#: The notice must actually be about a dividend. Word-bounded so "Dividends" matches but a
#: substring inside another word does not.
_DIVIDEND_RE = re.compile(r"\bdividend", re.IGNORECASE)

#: "Rs 1.50 Per Share", "Rs.1.50/-", "Re. 1 Per Share". The Rs/Re alternation is the same
#: Indian-English singular/plural distinction ``_SPLIT_RE`` already documents -- "Re." for
#: exactly one rupee, "Rs." otherwise -- and missing it would silently drop every Re. 1
#: dividend, which is a common declaration.
#:
#: A BARE NUMBER IS DELIBERATELY NOT MATCHED. Requiring the currency marker is what stops
#: this regex reading a ratio, a face value, a percentage or a year as an amount. It costs
#: the occasional real notice phrased without one; a false negative here produces no
#: candidate, which is recoverable, while a false positive produces a wrong rupee figure
#: attached to a real security.
_DIVIDEND_AMOUNT_RE = re.compile(r"\b(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

#: Which kind the notice says it is. Matched independently so a subject naming two kinds
#: can be detected as ambiguous rather than silently resolved to whichever matched first.
_DIVIDEND_KINDS = (
    ("INTERIM", re.compile(r"\binterim\b", re.IGNORECASE)),
    ("FINAL", re.compile(r"\bfinal\b", re.IGNORECASE)),
    ("SPECIAL", re.compile(r"\bspecial\b", re.IGNORECASE)),
)

#: Above this many named amounts, a subject reads as regex noise rather than a genuine
#: multi-tranche declaration -- NSE has never been observed naming more than two in one
#: notice. Bounding this also keeps the permutation count (n!) small enough for one
#: Choice call to enumerate as options.
_MAX_ATTRIBUTABLE_AMOUNTS = 3

#: Jev's own confidence guidance treats anything below this as genuinely unsure --
#: "don't guess" -- so this is the API's stated floor, not a number this codebase tuned.
#: A notice rejected below it is exactly as recoverable as before this module could ask
#: Jev at all: it stays unparsed, and a human still has the raw subject in the logs.
_JEV_CONFIDENCE_FLOOR = Decimal("0.5")


def _slash_pairs(subject: str) -> list[tuple[Decimal, str]] | None:
    """NSE writes several dividends in one notice as "/"-separated parts -- "Dividend - Rs
    30 Per Share/Special Dividend - Rs 20 Per Share" (INDIAMART 2025-06-06). When EVERY part
    that carries an amount carries exactly one amount, at most one kind and the word
    "dividend", the text itself pairs each amount with its kind: no ordering inference,
    which is what refusal 2 exists to avoid (owner decision 2026-10-13). A part with no
    amount ("Annual General Meeting") is context. Two parts of one kind, or fewer than
    two dividends, is not this shape -> None, and the ordinary refusals apply."""
    pairs: list[tuple[Decimal, str]] = []
    for part in subject.split("/"):
        amounts = _valid_amounts(_DIVIDEND_AMOUNT_RE.findall(part))
        if not amounts:
            continue
        kinds = [name for name, pattern in _DIVIDEND_KINDS if pattern.search(part)]
        if len(amounts) != 1 or len(kinds) > 1 or not _DIVIDEND_RE.search(part):
            return None
        pairs.append((amounts[0], kinds[0] if kinds else "UNSPECIFIED"))
    kinds_named = [kind for _, kind in pairs]
    if len(pairs) < 2 or len(set(kinds_named)) != len(kinds_named):
        return None
    return pairs


#: "Rights 3:25 @ Premium Rs 1799/-", "Rights 1:2@ Premium Re 1/-" -- the only shape NSE's
#: corporate-actions feed has been seen to use (checked across the 2022-2026 sweep).
_RIGHTS_RE = re.compile(
    r"^\s*rights\s+(\d+)\s*:\s*(\d+)\s*@\s*premium\s+r[se]\.?\s*(\d+(?:\.\d+)?)\s*/-\s*$",
    re.IGNORECASE,
)


def parse_rights_subject(subject: str, face_value: str) -> tuple[int, int, Decimal] | None:
    """NSE's rights notice -> ``(new, per_held, issue_price)``: ``new`` shares for every
    ``per_held`` held, at face value plus premium. ``None`` for any other wording or an
    unusable face value -- a rights factor is never computed from a guessed term."""
    match = _RIGHTS_RE.match(subject or "")
    if match is None:
        return None
    try:
        face = Decimal(str(face_value).strip())
    except (InvalidOperation, ValueError):
        return None
    new, per_held = int(match.group(1)), int(match.group(2))
    if face <= 0 or new <= 0 or per_held <= 0:
        return None
    return new, per_held, face + Decimal(match.group(3))


def _valid_amounts(raw_amounts: list[str]) -> list[Decimal]:
    """The regex can match a well-formed but nonsensical figure (a stray "Rs 0" in a
    disclaimer clause). Parsing failure or non-positivity drops that ONE candidate rather
    than the whole notice -- the same "one bad row must not cost the others" posture
    ``llm_verify`` documents for a different layer of this same problem.
    """
    valid: list[Decimal] = []
    for text in raw_amounts:
        try:
            value = Decimal(text)
        except InvalidOperation:
            continue
        if value > 0:
            valid.append(value)
    return valid


def _attribute_with_jev(
    jev_client: Any,
    subject: str,
    isin: str,
    symbol: str,
    amounts: list[Decimal],
    kinds: list[str],
) -> list[tuple[Decimal, str]] | None:
    """Ask Jev which amount pairs with which named kind. Returns the resolved
    ``(amount, kind)`` pairs, or ``None`` when there is no client, the shapes don't fit a
    question this function knows how to ask, the call fails, or confidence does not clear
    ``_JEV_CONFIDENCE_FLOOR`` -- in every one of those cases the caller's existing refusal
    is unchanged from before this function existed.

    Never raises: a transport failure or a shape drift here degrades to the refusal this
    notice always had, not a broken feed refresh over a network hiccup. Mirrors the
    "log-and-continue" posture ``refresh_dividend_feed`` already applies per row -- except
    quieter, because an ambiguous notice is this module's routine, documented case, not an
    exceptional one worth a log line of its own.

    Only two shapes are handled, matching the two ambiguities ``parse_dividend_feed``'s
    docstring documents. Anything else (mismatched amount/kind counts, more amounts than
    ``_MAX_ATTRIBUTABLE_AMOUNTS``) returns ``None`` without spending a call: the caller
    already refuses those shapes on its own, and Jev would only be guessing at a pairing
    with no textual anchor to reason from.
    """
    if jev_client is None or len(kinds) <= 1:
        return None

    state = {
        "subject": subject,
        "isin": isin,
        "symbol": symbol,
        "amounts_per_share": [str(a) for a in amounts],
        "kinds_named": kinds,
    }

    if len(amounts) == 1:
        criteria = {
            kind: f"Rs {amounts[0]} Per Share is the {kind.title()} Dividend" for kind in kinds
        }
        instructions = (
            "This NSE dividend notice's subject names one rupee amount and more than one "
            "kind of dividend (interim/final/special). Given the wording and order of "
            "`subject`, which named kind does the amount belong to?"
        )
    elif len(amounts) == len(kinds) and len(amounts) <= _MAX_ATTRIBUTABLE_AMOUNTS:
        pairings = list(permutations(kinds))
        criteria = {
            str(index): ", ".join(
                f"{kind.title()} Dividend = Rs {amount} Per Share"
                for kind, amount in zip(pairing, amounts, strict=True)
            )
            for index, pairing in enumerate(pairings)
        }
        instructions = (
            "This NSE dividend notice's subject names multiple rupee amounts and the same "
            "number of dividend kinds (interim/final/special), each amount attached to a "
            "kind somewhere in the wording. Which pairing of amount to kind is correct?"
        )
    else:
        return None

    try:
        answer = jev_client.choose(state, instructions, criteria)
    except (JevUnavailable, JevResponseError):
        return None

    if answer.confidence < _JEV_CONFIDENCE_FLOOR:
        return None

    if len(amounts) == 1:
        return [(amounts[0], answer.choice)]
    return list(zip(amounts, pairings[int(answer.choice)], strict=True))


def parse_dividend_feed(
    payload: bytes, jev_client: Any = None
) -> list[DividendFeedRow]:
    """NSE's ``corporates-corporateActions`` JSON into structured, unverified dividends.

    Reads the SAME payload ``parse_corporate_action_feed`` reads, so one fetch serves both
    and the anti-bot session warm-up is paid once.

    ``jev_client`` is an ``app.ai.jev.JevClient`` (or anything with the same ``choose``),
    injected rather than constructed here so this module needs no Settings and no network
    to test -- same posture as ``llm_extract.structure``'s injected OpenRouter client. ``None``,
    the default, is the ordinary state absent a configured key, and reproduces this
    function's behaviour from before Jev existed exactly: refusals 2 and 3 below always
    drop the notice rather than ask anything.

    FOUR THINGS THIS CAN REFUSE TO PARSE, each one a real shape in live NSE data:

    1. A PERCENTAGE DECLARATION -- "Dividend - 150%". Historically common, and it means
       150% OF FACE VALUE, not of price and not a rupee amount. Computing it needs the
       security's face value AT THAT TIME, which this feed does not carry and which a
       later split changes. Reading "150" as Rs 150 would be a 100x error on a Rs 1.50
       dividend. No candidate. Always refused; Jev is never asked about a percentage.
    2. MORE THAN ONE AMOUNT in one subject -- "Interim Dividend Rs 1.50 Per Share And
       Special Dividend Rs 2 Per Share". Both amounts and both kinds are present, but
       PAIRING them correctly is an inference about text order, and getting it backwards
       silently swaps a Rs 2 special with a Rs 1.50 interim. With a confident
       ``jev_client`` answer this now yields ONE ROW PER PAIR -- both are usually real
       declarations, not a choice between them. Without one (or below
       ``_JEV_CONFIDENCE_FLOOR``), still no candidate: a rejected notice is exactly as
       recoverable as before, and a human still has the raw subject in the logs.
    3. MORE THAN ONE KIND NAMED against a single amount, for the same reason -- which kind
       the one amount belongs to is an inference ``jev_client`` can also attempt, yielding
       one row on success and no candidate otherwise.
    4. ANYTHING ``_DISQUALIFYING_TERMS`` CATCHES, reused unchanged. A preference-share or
       debenture "dividend" is a different instrument's income and must not land against
       an equity ISIN; a scheme of arrangement is not a plain dividend. Always refused.

    ``recDate`` is read when present and ignored when absent or "-", which NSE sends for a
    date not yet fixed. It is never required: ``_REQUIRED_FIELDS`` deliberately does not
    include it, so a feed that stops sending it degrades to a missing display field rather
    than a format error that stops the run.

    Raises:
        CorporateActionFeedFormatError: same contract and same required fields as
            ``parse_corporate_action_feed`` -- the payload is not a JSON array of objects,
            or a row is missing a field every row has always carried.
    """
    import json

    try:
        records = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorporateActionFeedFormatError(f"not valid JSON: {exc}") from exc
    if not isinstance(records, list):
        raise CorporateActionFeedFormatError(f"expected a JSON array, got {type(records).__name__}")

    out: list[DividendFeedRow] = []
    for i, record in enumerate(records):
        if not isinstance(record, dict) or not _REQUIRED_FIELDS <= set(record):
            raise CorporateActionFeedFormatError(
                f"row {i}: missing required field(s) {sorted(_REQUIRED_FIELDS - set(record))}"
            )
        isin = str(record["isin"]).strip()
        symbol = str(record["symbol"]).strip()
        subject = str(record["subject"]).strip()
        if not isin or not subject:
            continue  # blank rows are a known artefact of this feed, not a format break
        if not _DIVIDEND_RE.search(subject):
            continue  # not a dividend notice -- the bonus/split parser's business
        if not is_plain_notice(subject):
            continue  # a qualifier changes what this is -- see _DISQUALIFYING_TERMS
        if "%" in subject:
            continue  # percentage of face value -- refusal 1 above

        ex_date = parse_nse_date(str(record["exDate"]).strip())
        if ex_date is None:
            continue  # "-" is a real, routine value for a not-yet-fixed date

        amounts = _valid_amounts(_DIVIDEND_AMOUNT_RE.findall(subject))
        if not amounts:
            continue  # nothing to attribute -- the "none to read" half of refusal 2

        kinds = [name for name, pattern in _DIVIDEND_KINDS if pattern.search(subject)]

        if len(amounts) == 1 and len(kinds) <= 1:
            pairs = [(amounts[0], kinds[0] if kinds else "UNSPECIFIED")]
        elif (slash := _slash_pairs(subject)) is not None:
            pairs = slash  # each "/" part names its own amount and kind -- no inference
        elif len(amounts) == 1:
            attributed = _attribute_with_jev(jev_client, subject, isin, symbol, amounts, kinds)
            if attributed is None:
                continue  # refusal 3: two+ kinds named, neither attributable
            pairs = attributed
        elif len(kinds) == len(amounts):
            attributed = _attribute_with_jev(jev_client, subject, isin, symbol, amounts, kinds)
            if attributed is None:
                continue  # refusal 2: multiple amounts that cannot be paired
            pairs = attributed
        else:
            continue  # refusal 2: amount/kind counts don't even match -- no textual
            # anchor for a pairing question, so not something jev_client is asked about

        record_date = parse_nse_date(str(record.get("recDate", "")).strip())
        if record_date is not None and record_date < ex_date:
            # A record date before the ex-date cannot be true and in practice means the
            # fields were transposed. Drop the date rather than the notice: the ex-date is
            # what eligibility uses, and record_date is display only.
            record_date = None

        for amount, kind in pairs:
            out.append(
                DividendFeedRow(
                    isin=isin,
                    symbol=symbol,
                    ex_date=ex_date,
                    amount_per_share=amount,
                    kind=kind,
                    record_date=record_date,
                    raw_subject=subject,
                )
            )

    return out
