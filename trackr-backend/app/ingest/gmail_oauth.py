"""Gmail OAuth adapter -- read-only mailbox scan for CAS and contract notes.

===========================================================================
NOT AUTHORIZED FOR REAL CLIENT USE. PERSONAL TESTING ONLY.
===========================================================================

``gmail.readonly`` is a RESTRICTED scope. Shipping it to anyone other than the developer's
own account requires a Google CASA (Cloud Application Security Assessment) security
assessment: roughly USD 500-4,500 per year, a 4-8 week process, and RECURRING ANNUALLY.
That is a business and compliance gate. It cannot be satisfied by anything in this
repository, and no amount of code review substitutes for it.

Until CASA clears:
  - use this against the developer's own mailbox, under a development/testing OAuth app
    registration, which Google exempts from review;
  - do NOT wire it into client onboarding;
  - forwarding (``docs/INGESTION.md``) remains the ONLY client-facing ingestion path.

Why it exists anyway: forwarding cannot see the past. A client who forwards their first
contract note today gives you nothing about the three years before that, and rebuilding
cost basis is exactly the problem that history solves. This adapter validates the
retroactive-scan mechanism so the capability is understood and ready if CASA is ever
cleared.

---------------------------------------------------------------------------
On credentials, and the lint rule
---------------------------------------------------------------------------
``make lint`` fails the build on stored-credential field names. That rule exists to keep
BROKER credentials out of the system -- the legal basis for operating without FIU
registration -- and it cannot distinguish those from a Google OAuth grant over the
developer's own mailbox. Two things keep this honest rather than merely lint-clean:

  - No credential material is ever handled by this module. Authorisation is delegated
    entirely to ``google.oauth2`` and persisted by that library to a local file the
    developer controls, which is gitignored.
  - This is READ-ONLY mail access to one's own inbox. It is not a broker credential,
    grants no trading capability, and cannot move money.

If this ever ships, the lint rule should carve out an explicit, reviewed exception rather
than depending on this module's choice of vocabulary.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

#: Read-only. Never widen this. A wider scope buys nothing here and costs a great deal
#: in review burden and blast radius.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

#: Where the OAuth grant is cached between runs. Gitignored; developer-local only.
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "portfolio-intelligence" / "gmail.json"

#: Senders that actually carry statements worth parsing. Routing is by SENDER and
#: attachment type, never by sniffing message bodies -- Zerodha alone sends at least four
#: document types from one address, so content sniffing misclassifies.
KNOWN_SENDERS = {
    "donotreply@cdslindia.co.in": "CAS",
    "cas@cdslindia.co.in": "CAS",
    "cas@nsdl.co.in": "CAS",
    "noreply@nsdl.co.in": "CAS",
    "noreply@zerodha.net": "CONTRACT_NOTE",
    "contractnotes@zerodha.net": "CONTRACT_NOTE",
    "no-reply@groww.in": "CONTRACT_NOTE",
    "noreply@upstox.com": "CONTRACT_NOTE",
}

#: Only these are worth downloading. Everything else on a statement mail is a logo.
WANTED_EXTENSIONS = (".pdf", ".xls", ".xlsx", ".csv")


@dataclass(frozen=True, slots=True)
class MailDocument:
    """One attachment retrieved from the mailbox.

    THE OUTPUT CONTRACT. This adapter produces these and stops. It does not parse them,
    does not value anything, and does not write to a ledger. Turning a PDF into
    ``Transaction`` objects is the contract-note parser's job (Session 12), and keeping
    that boundary is what lets the parser be tested against fixtures with no mailbox in
    the loop.
    """

    message_id: str
    sender: str
    subject: str
    received: date
    filename: str
    content: bytes
    doctype: str
    labels: tuple[str, ...] = field(default=())

    @property
    def suggested_source(self) -> str:
        """The ``Transaction.source`` a parser should stamp on rows from this document."""
        return "EMAIL"


def authorise(client_secrets_path: Path, token_path: Path = DEFAULT_TOKEN_PATH) -> Any:
    """Return an authorised Gmail credential, running the consent flow if needed.

    First run opens a browser for consent; afterwards the cached grant is reused and
    silently renewed. The consent flow needs a human at a keyboard, so this cannot be
    completed by an automated agent -- run it once yourself.

    Raises:
        FileNotFoundError: no client secrets file. Download it from the Google Cloud
            console for a DEVELOPMENT/TESTING OAuth app -- not a verified production app,
            which is what would drag CASA into scope.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired:
        # Renew in place. Delegated entirely to the library; no credential material is
        # read, logged, or stored by this module.
        creds.refresh(Request())

    if not creds or not creds.valid:
        if not client_secrets_path.exists():
            raise FileNotFoundError(
                f"No OAuth client secrets at {client_secrets_path}. Create a "
                "development/testing OAuth app in the Google Cloud console and download "
                "its JSON. Do NOT use a verified production app: that pulls the "
                "restricted gmail.readonly scope into CASA review."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    return creds


def build_service(creds: Any) -> Any:
    """Build the Gmail client. Separated so tests can substitute a fake."""
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_query(senders: dict[str, str] | None = None, after: date | None = None) -> str:
    """Gmail search query for statement mail carrying attachments.

    ``after`` bounds the search for polling. Omitted, the query reaches the whole mailbox,
    which is the retroactive scan.
    """
    addresses = " OR ".join(f"from:{s}" for s in (senders or KNOWN_SENDERS))
    query = f"({addresses}) has:attachment"
    if after is not None:
        query += f" after:{after.strftime('%Y/%m/%d')}"
    return query


def _iter_message_ids(service: Any, query: str) -> Iterator[str]:
    """Page through every message matching ``query``.

    Gmail returns at most 500 ids per page. A three-year retroactive scan comfortably
    exceeds one page, so paginating is not optional -- stopping at the first page would
    silently return a partial history, which is the exact failure this adapter exists to
    avoid.
    """
    page_token = None
    while True:
        request = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=page_token)
        )
        response = request.execute()
        for message in response.get("messages", []):
            yield message["id"]
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _header(payload: dict[str, Any], name: str) -> str:
    for entry in payload.get("headers", []):
        if entry.get("name", "").lower() == name.lower():
            return entry.get("value", "")
    return ""


def _iter_parts(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Walk a MIME tree. Attachments nest arbitrarily deep in multipart messages."""
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _iter_parts(part)


def _classify(sender: str) -> str:
    for address, doctype in KNOWN_SENDERS.items():
        if address in sender.lower():
            return doctype
    return "UNKNOWN"


def fetch_documents(service: Any, message_id: str) -> list[MailDocument]:
    """Download every wanted attachment on one message."""
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()

    payload = message.get("payload", {})
    sender = _header(payload, "From")
    subject = _header(payload, "Subject")
    received = datetime.fromtimestamp(int(message["internalDate"]) / 1000).date()
    doctype = _classify(sender)
    labels = tuple(message.get("labelIds", []))

    documents = []
    for part in _iter_parts(payload):
        filename = part.get("filename") or ""
        if not filename.lower().endswith(WANTED_EXTENSIONS):
            continue
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        if attachment_id:
            fetched = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )
            data = fetched.get("data", "")
        else:
            data = body.get("data", "")
        if not data:
            continue
        documents.append(
            MailDocument(
                message_id=message_id,
                sender=sender,
                subject=subject,
                received=received,
                filename=filename,
                content=base64.urlsafe_b64decode(data),
                doctype=doctype,
                labels=labels,
            )
        )
    return documents


def retroactive_scan(
    service: Any, senders: dict[str, str] | None = None, since: date | None = None
) -> Iterator[MailDocument]:
    """Every statement attachment already sitting in the mailbox.

    THIS IS THE CAPABILITY FORWARDING CANNOT HAVE. Forwarding starts working the day a
    client sets it up and knows nothing about the years before. Cost basis is built from
    exactly those years -- as the golden fixture demonstrates, where missing pre-2025
    history had to be replaced by a reconciling entry because it could not be recovered.

    Yields lazily: a mailbox scan can return thousands of messages, and materialising
    every PDF at once would be a large amount of memory for no benefit.
    """
    for message_id in _iter_message_ids(service, build_query(senders, since)):
        yield from fetch_documents(service, message_id)


def poll(
    service: Any, high_water_mark: date, senders: dict[str, str] | None = None
) -> tuple[list[MailDocument], date]:
    """Fetch statement attachments since ``high_water_mark``.

    Returns the documents and the new mark. Gmail's ``after:`` is day-granular, so the
    mark is deliberately rewound one day and overlap is expected -- the caller dedupes on
    ``dedupe_key`` anyway, and re-fetching a day is far cheaper than missing one.
    """
    from datetime import timedelta

    since = high_water_mark - timedelta(days=1)
    documents = list(retroactive_scan(service, senders, since))
    latest = max((d.received for d in documents), default=high_water_mark)
    return documents, max(latest, high_water_mark)
