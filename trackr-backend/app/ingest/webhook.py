"""Inbound-parse webhook handler for ``POST /ingest/email``.

docs/INGESTION.md "Adapter 3 -- Gmail forwarding": routing is by the ``to`` address
alone, NEVER by sniffing content -- Zerodha alone sends at least four document types
from one sender address, so content sniffing misclassifies and address routing cannot.

This module resolves investor + account + adapter from the address, unlocks a
password-protected PDF per the documented per-broker rule, and stages the parse via
``app.db.imports.stage_batch`` -- which itself stores the raw bytes before parsing, per
"Where documents land". It does not parse anything itself and does not decide source
precedence: it is glue over modules that already exist and are already tested.

Nothing here writes to the ledger. Staging only -- promotion is a separate, authenticated
call (``app/routers/ingest.py``), per "Staging: why nothing auto-commits".
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.db import consent, crypto, documents, forwarding_confirmation
from app.ingest.base import Adapter
from app.ingest.cas_pdf import CasPdfAdapter
from app.ingest.zerodha_contract_note import ZerodhaContractNoteAdapter
from app.jobs import queue

#: broker -> the case transform applied to the investor's PAN to unlock a contract-note
#: PDF. docs/INGESTION.md "PDF password rules -- a real trap": a single hardcoded case
#: silently breaks the brokers that disagree, so this stays a per-broker lookup.
PDF_PASSWORD_CASE = {
    "zerodha": str.upper,
    "groww": str.upper,
    "upstox": str.lower,
    # CDSL's own CAS FAQ: "PAN of the first/sole demat account holder in CAPITAL letters".
    # NSDL's eCAS follows the same rule. app.ingest.cas_pdf._password_candidates tries
    # variants on top of whatever it is handed, so this is the documented starting point
    # rather than the only thing attempted.
    "cdsl": str.upper,
    "nsdl": str.upper,
}

#: (broker, doctype) -> adapter factory, taking the unlocked PDF password. Only
#: combinations with a real, tested parser are listed -- CLAUDE.md: never invent parsing
#: logic for a source the codebase does not yet handle. A combination absent here is a
#: named gap, not a guess.
#: Every factory takes ``(password, broker_accounts)``. The second argument matters only
#: for a cross-broker document; the broker-specific adapters ignore it. Uniform on purpose
#: -- a registry whose entries have different arities is how a caller ends up silently
#: constructing one of them wrong.
_ADAPTER_FACTORIES: dict[tuple[str, str], Any] = {
    ("zerodha", "contractnote"): lambda password, _accounts: ZerodhaContractNoteAdapter(
        password=password
    ),
    # A CAS is a HOLDINGS AND DISCOVERY source, never a transaction source. It carries no
    # price -- a depository records securities movement, not trades -- so it can never
    # produce a Transaction, and app.ingest.cas_pdf enforces that by rejecting rather than
    # guessing. What it uniquely gives is every ISIN and quantity across EVERY broker on
    # one PAN, which is the only completeness proof this system can get. Registered for
    # both depositories because whichever holds the earlier-opened demat account is the
    # one that mails it (SEBI CIR/MRD/DP/31/2014).
    ("cdsl", "cas"): lambda password, accounts: CasPdfAdapter(
        pan=password, broker_accounts=accounts
    ),
    ("nsdl", "cas"): lambda password, accounts: CasPdfAdapter(
        pan=password, broker_accounts=accounts
    ),
}

#: Doctypes that describe the investor's whole portfolio rather than one broker's dealings.
#: A CAS spans every DP on a PAN, so requiring a matching ``account.broker`` row would make
#: it unroutable for exactly the investors it helps most. ``resolve_account`` therefore
#: falls back to any account of theirs, and app.ingest.cas_pdf re-resolves the real account
#: per DP from the document's own text (``CasPdfAdapter._resolve_account``).
_CROSS_BROKER_DOCTYPES = frozenset({"cas"})

#: Refuse a delivery carrying more attachments than this. Gmail's bulk "Forward as
#: attachment" is capped by its own page size of 100 conversations, so this is that plus
#: headroom -- high enough never to refuse a legitimate forward, low enough that someone
#: forwarding an entire mailbox does not enqueue ten thousand jobs. Refused with 406 so the
#: provider does not retry a permanent condition for eight hours.
MAX_ATTACHMENTS_PER_DELIVERY = 150


class TooManyAttachmentsError(ValueError):
    """One delivery carried more attachments than MAX_ATTACHMENTS_PER_DELIVERY."""


class UnroutableAddressError(ValueError):
    """The ``to`` address is not ``{investor_code}-{broker}-{doctype}@...``."""


class NoSuchInvestorError(LookupError):
    """No investor with this code."""


class NoMatchingAccountError(LookupError):
    """The investor has no account for this broker."""


class UnsupportedDocumentError(ValueError):
    """No adapter exists yet for this (broker, doctype) pair."""


@dataclass(frozen=True, slots=True)
class RoutedAddress:
    investor_code: str
    broker: str
    doctype: str


def parse_to_address(to: str) -> RoutedAddress:
    """``{investor_code}-{broker}-{doctype}@domain`` -> its three segments.

    ``investor_code`` itself may contain hyphens (e.g. ``TEST-FE54``, a real seeded
    code) -- ``broker``/``doctype`` never do, both coming from the fixed
    ``_ADAPTER_FACTORIES`` registry. Splitting from the right by exactly two hyphens
    keeps that last pair intact and folds everything else back into the code, so this
    stays the exact inverse of ``app.ingest.addresses.forwarding_addresses``.

    Raises:
        UnroutableAddressError: the local part is not three non-empty segments once
            split this way.
    """
    local = to.split("@", 1)[0]
    parts = local.rsplit("-", 2)
    if len(parts) != 3 or not all(parts):
        raise UnroutableAddressError(f"not a routable ingest address: {to!r}")
    code, broker, doctype = parts
    return RoutedAddress(code.strip().lower(), broker.strip().lower(), doctype.strip().lower())


def resolve_investor(conn: Any, investor_code: str) -> tuple[str, str]:
    """``(investor_id, investor_code)`` for the code in the address."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, code FROM investor WHERE lower(code) = %s", (investor_code,))
        row = cur.fetchone()
    if row is None:
        raise NoSuchInvestorError(investor_code)
    return str(row[0]), row[1]


def resolve_account(conn: Any, investor_id: str, broker: str, doctype: str = "") -> str:
    """The account this document's rows belong to.

    For a broker document that is the account matching the broker. For a cross-broker
    document -- a CAS, which is consolidated across every DP on one PAN -- there is no
    "cdsl account" to match, so the investor's first account (by id) only files the
    batch: ``CasPdfAdapter`` resolves the account every row and holding belongs to from
    the statement's own DP summary, and REFUSES the statement
    (``CasAccountUnresolvedError``) when it cannot -- it never lands on this one.
    Requiring a matching broker row here would make a CAS unroutable for every investor,
    which is the opposite of what it is for.
    """
    with conn.cursor() as cur:
        if doctype in _CROSS_BROKER_DOCTYPES:
            # ORDER BY id, not by age: ``account`` carries no timestamp column, so
            # "earliest" is not expressible. Deterministic is what actually matters here --
            # this only files the batch; the CAS adapter attributes every holding itself
            # from ``broker_accounts`` and refuses the statement when it cannot.
            cur.execute(
                "SELECT id FROM account WHERE investor_id = %s ORDER BY id LIMIT 1",
                (investor_id,),
            )
        else:
            cur.execute(
                "SELECT id FROM account WHERE investor_id = %s AND lower(broker) = %s LIMIT 1",
                (investor_id, broker),
            )
        row = cur.fetchone()
    if row is None:
        raise NoMatchingAccountError(f"investor has no account for broker {broker!r}")
    return str(row[0])


def raw_pan(conn: Any, investor_id: str) -> str:
    """The investor's PAN as stored, with no case transform applied. Empty string when
    none is on file. Exists as its own function (rather than inlined in
    ``pdf_password``) so app.routers.ingest's upload endpoints can resolve the same
    PAN without duplicating the investor_secret/crypto.decrypt query."""
    with conn.cursor() as cur:
        cur.execute("SELECT pan_enc FROM investor_secret WHERE investor_id = %s", (investor_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return ""
    return crypto.decrypt(row[0])


def pdf_password(conn: Any, investor_id: str, broker: str) -> str:
    """PAN, cased per broker. Empty string when no PAN is on file or the broker has no
    documented password rule -- unlocking then fails inside pdfplumber, which is the
    honest outcome. Guessing a password is worse than failing to open the file."""
    case = PDF_PASSWORD_CASE.get(broker)
    if case is None:
        return ""
    pan = raw_pan(conn, investor_id)
    return case(pan) if pan else ""


def investor_broker_accounts(conn: Any, investor_id: str) -> dict[str, str]:
    """``{broker: account_id}`` for every account this investor holds.

    What a CAS needs and a broker document does not. A consolidated statement lists each
    depository participant by name -- "ZERODHA BROKING LIMITED", "AXIS SECURITIES LIMITED"
    -- and ``CasPdfAdapter._resolve_account`` matches those names against this map so each
    DP's holdings are attributed to the right account. Without it every holding in a
    multi-broker statement lands on one arbitrary account, which would make the
    reconciliation the CAS exists to provide actively misleading.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT lower(broker), id FROM account WHERE investor_id = %s AND broker IS NOT NULL",
            (investor_id,),
        )
        return {row[0]: str(row[1]) for row in cur.fetchall()}


def adapter_for(conn: Any, investor_id: str, broker: str, doctype: str) -> Adapter:
    factory = _ADAPTER_FACTORIES.get((broker, doctype))
    if factory is None:
        raise UnsupportedDocumentError(f"no adapter for broker={broker!r} doctype={doctype!r}")
    accounts = (
        investor_broker_accounts(conn, investor_id)
        if doctype in _CROSS_BROKER_DOCTYPES
        else None
    )
    return factory(pdf_password(conn, investor_id, broker), accounts)


def handle_inbound_email(
    conn: Any,
    store: Any,
    to: str,
    attachments: list[dict[str, Any]],
    sender: str = "",
    body_text: str = "",
    max_attachments: int = MAX_ATTACHMENTS_PER_DELIVERY,
) -> list[dict[str, Any]]:
    """Archive every attachment on one inbound message and queue it for parsing.

    I/O ONLY. Nothing here parses a PDF, and that is the point. Mailgun's webhook timeout
    is 15 seconds and it retries any non-200 six times over eight hours, so a delivery of
    more than a handful of documents cannot be parsed inside the request -- a slow webhook
    is not slow, it is the same message delivered again, on a backoff, for hours. Measured:
    a contract note is ~0.26 s, so about 55 of them crosses 15 s, and a bulk forward
    carries up to 100. See docs/BACKFILL_PIPELINE.md sections 4.1 and 5.

    So each attachment is stored (bytes to object storage, a row in ``document`` with
    ``parse_status='UNPARSED'``) and a job is enqueued naming it. Both writes share this
    request's single transaction, so a 200 is never returned for work that was not durably
    recorded -- the reason the queue is Postgres and not a separate broker.

    The routing checks that are CHEAP still happen here, before anything is stored: a
    document whose address does not resolve, whose investor or account does not exist, or
    for which no adapter is registered, is refused at the door. Only the expensive
    judgement -- what is actually inside the file -- is deferred.

    Returns one result per attachment, shaped like ``stage_batch``'s so the response model
    is unchanged: a queued attachment reports its ``document_id`` and no ``batch_id``,
    because no batch exists until it is parsed.

    Raises:
        UnroutableAddressError, NoSuchInvestorError, NoMatchingAccountError,
            UnsupportedDocumentError: routing failed before anything was stored.
        TooManyAttachmentsError: more attachments than this deployment accepts in one
            delivery. Permanent, so the router answers 406 rather than inviting a retry.
        ConsentMissingError: routing succeeded but this investor has no live consent --
            checked last, after routing, so those errors keep naming the actual routing
            problem instead of being masked by a 403.
    """
    routed = parse_to_address(to)
    investor_id, investor_code = resolve_investor(conn, routed.investor_code)

    # Gmail's forwarding verification, handled before anything document-shaped.
    #
    # ORDER MATTERS THREE WAYS. It is after resolve_investor because a code is worthless
    # unless we know whose address it was sent to. It is before resolve_account and
    # adapter_for because this message is not a document and must not be refused for
    # having no parser -- that is exactly the 422 that used to swallow it. And it is
    # before require_consent because a mailbox-verification handshake is not processing
    # anyone's financial data: gating it on DPDP consent would deadlock setup for an
    # investor who has not granted yet, and there is nothing here to protect -- the code
    # is generated by the client's own Gmail and is shown straight back to them.
    #
    # Returns [] rather than raising on an unreadable body: Gmail sends other mail from
    # this address, and a non-2xx would make the provider retry a permanent non-event for
    # hours (docs/ONBOARDING_FLOW.md, defect 9).
    if sender.strip().lower() == forwarding_confirmation.GMAIL_FORWARDING_SENDER:
        code = forwarding_confirmation.extract_code(body_text)
        if code is not None:
            forwarding_confirmation.record_confirmation_code(
                conn, investor_id, routed.broker, routed.doctype, code
            )
        return []

    if len(attachments) > max_attachments:
        raise TooManyAttachmentsError(
            f"delivery carries {len(attachments)} attachments; this address accepts at most "
            f"{max_attachments} per message. Forward in smaller batches -- re-sending a "
            f"batch you already sent is a no-op, so nothing is lost by overlapping."
        )

    account_id = resolve_account(conn, investor_id, routed.broker, routed.doctype)
    # Resolved here purely to REFUSE EARLY for an unregistered (broker, doctype): the
    # instance is thrown away, and the drain builds its own at parse time. Doing it here
    # keeps "no parser for this" a 406 at the door rather than a queued job that can only
    # ever fail.
    adapter_for(conn, investor_id, routed.broker, routed.doctype)
    consent.require_consent(conn, investor_id)

    # One delivery, one id. This is what turns "promote 487 documents" into a single
    # attributed decision instead of 487 of them (migration 038).
    delivery_id = str(uuid.uuid4())
    doctype = routed.doctype.upper()

    results: list[dict[str, Any]] = []
    for attachment in attachments:
        content = base64.b64decode(attachment["content_b64"])
        filename = attachment.get("filename", "")
        # Present only when this attachment came out of a nested message/rfc822 part --
        # i.e. a bulk "Forward as attachment". These are the ORIGINAL email's headers, not
        # the forwarding wrapper's, which is what makes a date-window lookup over the
        # archive meaningful later.
        email_meta = attachment.get("email_meta")

        stored = documents.store_document(
            conn,
            store,
            investor_id=investor_id,
            investor_code=investor_code,
            doctype=doctype,
            content=content,
            received=date.today(),
            filename=filename,
            content_type=attachment.get("content_type") or "application/pdf",
            account_id=account_id,
            source_message_id=(email_meta or {}).get("message_id"),
            actor_uid=None,
            email_meta=email_meta,
        )
        document_id = str(stored["id"])
        sha256 = str(stored["sha256"])

        if stored.get("already_present"):
            # Content addressing, per investor (migration 020). Re-forwarding a Gmail page
            # you already sent is explicitly supported -- users are told "if you lose your
            # place, just forward the page again" -- and this is what makes that true
            # rather than hopeful. No second job: the first one already covers these bytes.
            results.append(
                {
                    "batch_id": None,
                    "parsed": 0,
                    "rejected": 0,
                    "sha256": sha256,
                    "document_id": document_id,
                    "notes": [f"skipped: {filename or 'document'} already archived"],
                }
            )
            continue

        queue.enqueue(
            conn,
            queue.KIND_PARSE_DOCUMENT,
            {
                # IDS ONLY -- no bytes, no PAN. app/jobs/queue.py refuses anything else.
                "document_id": document_id,
                "account_id": account_id,
                "investor_code": investor_code,
                "broker": routed.broker,
                "doctype": routed.doctype,
                "filename": filename,
                "delivery_id": delivery_id,
            },
            investor_id=investor_id,
        )
        results.append(
            {
                "batch_id": None,
                "parsed": 0,
                "rejected": 0,
                "sha256": sha256,
                "document_id": document_id,
                "notes": [f"queued for parsing: {filename or 'document'}"],
            }
        )
    return results
