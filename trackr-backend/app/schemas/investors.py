"""DTOs for POST /investors and POST /investors/{id}/accounts -- onboarding.

Mirrors contracts/openapi.yaml's inline request/response schemas for these two paths
field for field, the same convention every other schemas/*.py file in this package
follows rather than a shared components/schemas section.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class InvestorIn(BaseModel):
    """POST /investors request body. ``pan`` is optional and write-only: if supplied,
    it is routed through ``app.db.repository.create_investor`` into ``app.db.crypto``
    and is never stored on ``investor`` or echoed back in ``InvestorOut`` -- CLAUDE.md's
    rule that PAN never lands on the investor row. No pattern is enforced here on
    purpose; ``app.db.crypto.normalise_pan`` is the one place that check lives, and its
    failure is what turns into this endpoint's 400.

    ``onboarding_date`` is when the advisory relationship with this client began -- it
    feeds ``compute_period_xirr``'s ``start_date`` (app.engine.xirr) via GET
    /investors/{id}/performance's ``period_xirr``. Optional and nullable: when unset,
    that metric is simply omitted, never guessed.

    ``code`` is optional and normally left out: Trackr+ then generates one
    (``app.db.repository.new_investor_code``), so nobody has to invent a globally unique
    code. A caller that sends one keeps the old behaviour."""

    code: str | None = None
    display_name: str
    family_group_id: str | None = None
    pan: str | None = None
    onboarding_date: date | None = None


class InvestorOut(BaseModel):
    id: str
    code: str
    display_name: str
    family_group_id: str | None = None
    onboarding_date: date | None = None
    created_at: datetime


class SetPanIn(BaseModel):
    """PUT /investors/{id}/pan request body. Write-only, same as InvestorIn.pan --
    never echoed back in any response."""

    pan: str


class FilePasswordOut(BaseModel):
    """GET /investors/{id}/file-passwords -- one row per statement source. Says whether a
    password is saved, never what it is: there is no route that returns one."""

    source: str
    label: str
    hint: str
    saved: bool
    updated_at: datetime | None = None


class AccountIn(BaseModel):
    """POST /investors/{id}/accounts request body."""

    broker: str
    dp_id: str | None = None
    client_id: str | None = None
    is_advised: bool = True
    label: str | None = None


class AccountOut(BaseModel):
    id: str
    investor_id: str
    broker: str
    dp_id: str | None = None
    client_id: str | None = None
    is_advised: bool
    label: str | None = None
    # One address per (broker, doctype) pair with a real adapter -- see
    # app.ingest.addresses.forwarding_addresses. Empty for a broker with no adapter
    # yet, never a fabricated address for one that doesn't exist.
    forwarding_addresses: list[str] = []


class BrokerOut(BaseModel):
    """One entry of GET /brokers -- app.brokers.describe's output.

    Every capability field is DERIVED on the server from a registry a real parser is
    already in (app/brokers.py's module docstring says which). None of them may be
    overridden per deployment, and none may be set optimistically: a picker that promises
    a parser that does not exist is the exact failure this endpoint was added to stop
    (docs/ONBOARDING_AS_IS_VS_TO_BE.md, loophole L7).
    """

    code: str
    name: str
    #: Document kinds a deterministic adapter handles -- "tradebook_csv",
    #: "contract_note_pdf", ... Empty is a valid, honest answer.
    documents: list[str] = []
    parses_contract_notes: bool = False
    parses_tradebooks: bool = False
    #: The same human phrases a rejected upload uses, so the promise and the failure
    #: message cannot describe the same file differently.
    document_labels: list[str] = []
    #: True only when this broker has an email adapter AND this deployment has a
    #: mail-receiving domain. False means no address exists to show, not "not yet".
    forwarding: bool = False
    #: Whether an email adapter exists for this broker at all, independent of the
    #: deployment's mail domain. With ``forwarding`` false, this is what says whether the
    #: missing address is our gap (no parser) or the deployment's (no inbound mail).
    email_adapter: bool = False
    #: How this broker locks contract-note PDFs, in a sentence. "" when we have no
    #: documented rule for it -- which is a thing to say, not a thing to guess.
    pdf_password: str = ""
    console_steps: list[str] = []


class OnboardingStateOut(BaseModel):
    """GET /investors/{id}/onboarding -- how far along this investor is, derived on read.

    THE POINT OF THIS ENDPOINT is that there is no ``setup_complete`` column and there
    must never be one. CLAUDE.md's one architectural rule is that state is derived rather
    than stored, and onboarding progress already IS derivable from facts the database
    holds for other reasons. Storing a duplicate boolean beside them is how the two start
    disagreeing.

    IT REPLACES A localStorage KEY, which is the bug it exists to fix: "they finished
    setup" used to live in one browser, so the same account on a phone was offered the
    wizard again -- and offered to create a second broker account with it
    (docs/ONBOARDING_AS_IS_VS_TO_BE.md section 4, loophole L4).
    """

    #: Terms + Privacy accepted at registration. False for accounts created before
    #: app_user.tos_version existed -- never back-filled (migration 021).
    terms_accepted: bool
    #: Live DPDP consent for this investor. Ingestion 403s without it.
    consent: bool
    accounts: int
    transactions: int
    #: Whether this deployment can offer email forwarding at all
    #: (app.ingest.addresses.forwarding_enabled). When False the wizard omits that step
    #: entirely rather than showing an address that cannot receive mail.
    forwarding_available: bool
    #: consent AND an account AND at least one transaction. Forwarding is deliberately
    #: NOT part of this: declining it is a finished setup, not an abandoned one
    #: (docs/ONBOARDING_AS_IS_VS_TO_BE.md section 6).
    complete: bool
    #: The first unfinished step -- "permission", "brokers", "history" -- or None when
    #: complete. What the wizard opens on, so it resumes rather than restarts.
    next_step: str | None = None


class ForwardingCodeOut(BaseModel):
    """One Gmail forwarding verification code, for GET /investors/{id}/forwarding-code.

    Gmail will not forward to an address until a code it mails to that address is pasted
    back into Gmail's own settings. Nothing in this product can paste it for the person,
    and auto-clicking the confirmation link would be us granting a mail permission on
    their behalf -- so the honest shape is to catch the code and hand it straight back.
    See migration 037 for why this is evidence rather than a credential.
    """

    #: Lowercase, as the address segments are parsed -- app.ingest.webhook.parse_to_address.
    broker: str
    doctype: str
    code: str
    #: When the verification mail arrived. Shown so a person can tell a code they are
    #: waiting for from one left over from an earlier attempt.
    received_at: datetime
