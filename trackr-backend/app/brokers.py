"""The broker registry: one list, served by the backend, that the UI may not second-guess.

WHY THIS EXISTS. Three separate places used to hold their own idea of what a broker is --
the onboarding wizard hardcoded ``"ZERODHA"``, the Import screen hardcoded the label
"Zerodha tradebook (CSV)", and the file-upload help text named Zerodha Console. Each was
right about Zerodha and silently wrong about everyone else, and a Groww user was handed a
Zerodha account without ever being asked (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loophole L7).
The backend is the only side that knows which adapters actually exist, so it is the only
side that may answer "what can we do with this broker's files".

THE BADGES MUST NOT FLATTER. A picker that lists thirty brokers and fails silently on
twenty-nine is not a multi-broker product. So every capability on the wire is DERIVED from
a registry that a real, tested parser is already in:

    ``documents``  <- app.ingest.file_validation.ROUTING, the deterministic upload path
    ``forwarding`` <- app.ingest.addresses, which reads app.ingest.webhook._ADAPTER_FACTORIES
    ``pdf_password`` <- app.ingest.webhook.PDF_PASSWORD_CASE, by applying the transform

None of those three is retyped here, so none of them can drift. A broker declared with no
adapters is a valid, honest entry: the account exists, files are accepted and classified,
unrecognised ones are refused with a sentence, and forwarding is simply not offered.

WHAT A BROKER ENTRY MAY CLAIM. ``adapters`` names adapters that exist -- tests/
test_brokers.py fails the build if an entry names one that does not. Everything else in
an entry is prose for a human: where to download files, and how the broker locks its
PDFs. Prose cannot promise a parser, which is the whole point.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ingest.addresses import broker_has_email_adapter, broker_has_forwarding
from app.ingest.file_validation import LABELS, ROUTING
from app.ingest.webhook import PDF_PASSWORD_CASE

#: A structurally valid PAN whose case is mixed, so applying a broker's password
#: transform to it reveals which direction that transform goes. Never a real PAN, and
#: never stored -- it exists only to read a rule that is already written in code.
_SAMPLE_PAN = "AbCdE1234F"


@dataclass(frozen=True, slots=True)
class Broker:
    """One broker we are willing to name in the UI.

    ``adapters`` holds adapter names from ``app.ingest.file_validation.ROUTING`` -- the
    deterministic upload registry. An empty tuple is the honest answer for a broker whose
    documents nothing here parses yet, and is what makes "files ✅ / email —" a truthful
    row rather than a hopeful one.
    """

    code: str
    name: str
    #: Deterministic upload adapters for this broker's documents. Validated against
    #: ROUTING by tests/test_brokers.py -- a typo here is a build failure, not a silent
    #: capability that never materialises.
    adapters: tuple[str, ...] = ()
    #: Where this broker's files come from, in the person's own steps. Prose only.
    console_steps: tuple[str, ...] = field(default_factory=tuple)


#: Every broker the picker offers. Ordered by how likely a first-time Indian retail
#: investor is to be with them, not alphabetically -- the list is read top-down and the
#: common case belongs first.
#:
#: ADDING ONE is deliberately cheap and deliberately does not imply a parser: give it a
#: code, a name and console steps, leave ``adapters`` empty, and it works end to end as a
#: files-accepted account. Filling ``adapters`` in is a separate piece of work that needs
#: real sample documents, and must never be done in anticipation.
BROKERS: tuple[Broker, ...] = (
    Broker(
        code="ZERODHA",
        name="Zerodha",
        adapters=("zerodha_tradebook", "zerodha_contract_note_xlsx", "zerodha_contract_note"),
        console_steps=(
            "Sign in to Zerodha Console (console.zerodha.com).",
            "For trades: Reports → Tradebook, pick a year, download the CSV. Console caps "
            "each download at one year, so grab one file per year.",
            "For costs: Reports → Contract notes, pick a date range, download. Contract "
            "notes are the only file carrying brokerage and taxes.",
        ),
    ),
    Broker(
        code="GROWW",
        name="Groww",
        console_steps=(
            "Sign in to Groww on the web (groww.in).",
            "Open your profile → Reports → Stocks.",
            "Download the tradebook or contract notes for the period you want.",
        ),
    ),
    Broker(
        code="UPSTOX",
        name="Upstox",
        console_steps=(
            "Sign in to Upstox on the web (upstox.com).",
            "Open Reports → Trades, or Reports → Contract notes.",
            "Pick a date range and download.",
        ),
    ),
    Broker(
        code="ANGELONE",
        name="Angel One",
        console_steps=(
            "Sign in to Angel One on the web (angelone.in).",
            "Open Reports → Trade book, or Reports → Contract note.",
            "Pick a date range and download.",
        ),
    ),
    Broker(
        code="ICICIDIRECT",
        name="ICICI Direct",
        console_steps=(
            "Sign in to ICICI Direct (icicidirect.com).",
            "Open Portfolio → Reports, or Trading → Digital contract notes.",
            "Pick a date range and download.",
        ),
    ),
)


def _documents_for(adapters: tuple[str, ...]) -> tuple[str, ...]:
    """The document kinds these adapters actually claim, read out of ``ROUTING``.

    Inverting ROUTING rather than declaring the kinds on the Broker is what stops the two
    from disagreeing: a document kind appears here only because a real adapter is
    registered against it on the upload path.
    """
    by_adapter = {adapter: document for document, (adapter, _route) in ROUTING.items()}
    return tuple(by_adapter[a] for a in adapters if a in by_adapter)


def _pdf_password_rule(code: str) -> str:
    """How this broker locks its contract-note PDFs, in a sentence, or "" when we have no
    documented rule.

    Derived by APPLYING the broker's own transform to a sample PAN rather than by keeping
    a second table of sentences beside ``PDF_PASSWORD_CASE``. docs/INGESTION.md calls the
    per-broker casing "a real trap" precisely because the brokers disagree; a description
    that could drift from the transform it describes would be the same trap one level up.
    """
    case = PDF_PASSWORD_CASE.get(code.strip().lower())
    if case is None:
        return ""
    cased = case(_SAMPLE_PAN)
    if cased.isupper():
        return "Your PAN, in capitals"
    if cased.islower():
        return "Your PAN, in lower case"
    return "Your PAN"


def describe(broker: Broker, ingest_domain: str) -> dict[str, object]:
    """One broker as the wire sees it -- capabilities derived, never declared.

    ``ingest_domain`` decides ``forwarding`` alongside the adapter registry, because both
    have to be true for an address to be worth showing: a broker with an email adapter on
    a deployment with no mail-receiving domain has no address, and offering one anyway is
    how a dead ``@ingest.example.com`` reached a real screen.
    """
    documents = _documents_for(broker.adapters)
    return {
        "code": broker.code,
        "name": broker.name,
        "documents": list(documents),
        # The two badges the picker draws, resolved here rather than by the client
        # pattern-matching on document-kind strings it would have to keep in sync.
        "parses_contract_notes": any(d.startswith("contract_note") for d in documents),
        "parses_tradebooks": any(d.startswith("tradebook") for d in documents),
        # What each parsed kind is, in words -- the same phrases a rejected upload uses,
        # so the promise made in the picker and the sentence shown on failure match.
        "document_labels": [LABELS[d] for d in documents if d in LABELS],
        "forwarding": broker_has_forwarding(ingest_domain, broker.code),
        # The domain-independent half of "forwarding", so a client can say WHICH of
        # the two reasons an address is missing -- see broker_has_email_adapter.
        "email_adapter": broker_has_email_adapter(broker.code),
        "pdf_password": _pdf_password_rule(broker.code),
        "console_steps": list(broker.console_steps),
    }


def registry(ingest_domain: str) -> list[dict[str, object]]:
    """The whole registry, described. What ``GET /brokers`` returns."""
    return [describe(broker, ingest_domain) for broker in BROKERS]
