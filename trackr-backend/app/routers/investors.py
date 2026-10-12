"""POST /investors, POST /investors/{id}/accounts -- onboarding.

Every other endpoint in this API takes an ``investor_id`` it assumes already exists.
Before this router, the only way one did was ``app.db.load_golden``'s one-fixture
seed -- an adviser had no way to onboard a second client without hand-writing SQL. See
docs/FULL_VISION_BACKEND_ROADMAP.md section A5.

Onboarding is authenticated and attributed: the creating login becomes
``investor.owner_user_id``, which is what every scoped read authorises against. An
investor created without an owner would be unreachable through the API.

All logic lives in ``app.db.repository``; these handlers only translate between the
wire shape and the repository calls, the same split ``app.routers.transactions`` uses.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import get_current_user, require_owned_investor
from app.config import Settings, get_settings
from app.database import get_conn, money, rate
from app.db import consent, families, forwarding_confirmation, repository
from app.db.repository import create_account, create_investor, set_investor_pan
from app.engine.errors import EngineError
from app.engine.holdings import compute_holdings_partitioned
from app.engine.types import ZERO
from app.ingest.addresses import forwarding_addresses, forwarding_enabled
from app.prices.current import resolve_current_prices
from app.routers.holdings import value_partition
from app.schemas.families import ClientsOverview
from app.schemas.investors import (
    AccountIn,
    AccountOut,
    ForwardingCodeOut,
    InvestorIn,
    InvestorOut,
    OnboardingStateOut,
    SetPanIn,
)

router = APIRouter(prefix="/investors", tags=["onboarding"])


@router.get("", response_model=list[InvestorOut])
def list_investors(
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Every portfolio this login owns, oldest first.

    THE ENDPOINT THAT MAKES SWITCHING POSSIBLE. Creating a second investor has always
    worked, and /auth/login has always echoed back exactly one -- the oldest
    (``first_investor_id_for_user``). With no way to enumerate the rest, a login that
    owned two portfolios could reach the second only in the browser session that created
    it: log out, log back in, and the session snapped to the first with the second
    unreachable from any screen. Nothing was lost, and nothing would admit it was there.

    Scoped by ownership rather than by a path parameter, so there is no investor id to
    authorise -- the query itself is the authorisation, and a login sees its own rows or
    an empty list. That is why this is the one investor route with no
    ``require_owned_investor``.
    """
    return repository.list_investors_for_user(connection, str(current_user["id"]))


def _figures(units: list[dict[str, Any]], prices: dict, as_of: date) -> None:
    """Fill each unit's totals in place, over PRICED holdings only -- the same split and
    the same arithmetic as /summary (``value_partition`` + ``holdings._totals``)."""
    for unit in units:
        out = unit["out"]
        if out.get("error"):
            continue
        try:
            valued, unpriced = value_partition(unit["holdings"], prices, as_of)
        except EngineError as exc:
            out["error"] = str(exc)
            continue
        invested = sum((h.invested for h in valued), ZERO)
        market_value = sum((h.market_value for h in valued), ZERO)
        unrealised = market_value - invested
        out.update(
            invested=money(invested),
            market_value=money(market_value),
            unrealised=money(unrealised),
            unrealised_pct=rate(unrealised / invested) if invested != ZERO else None,
            unpriced_count=len(unpriced),
        )


@router.get("/overview", response_model=ClientsOverview)
def overview(
    as_of: date | None = None,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Every client this login manages, with each broker account under it, valued.

    The adviser's clients view (Settings -> Portfolios). Scoped by ownership like
    ``list_investors``, so there is no investor id to authorise.

    A CLIENT'S figures are exactly its Dashboard's /summary figures: the whole ledger,
    priced holdings only, unpriced and non-reconciling stocks counted and left out.
    An ACCOUNT'S figures replay only that account's own trades. Under average cost a
    stock held in two accounts can make the accounts not sum exactly to the client, so a
    client total is never derived from its accounts.

    Prices are resolved ONCE for every stock any client holds. A client or account whose
    ledger cannot be valued carries ``error`` and no figures; the rest still return.
    """
    user_id = str(current_user["id"])
    on = as_of or date.today()
    investors = repository.list_investors_for_user(connection, user_id)

    clients: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    held: set[str] = set()

    def unit(out: dict[str, Any], transactions: list) -> None:
        try:
            holdings, skipped = compute_holdings_partitioned(transactions, as_of=on)
        except EngineError as exc:
            out["error"] = str(exc)
            holdings, skipped = [], []
        out["skipped_count"] = len(skipped)
        held.update(h.isin for h in holdings)
        units.append({"out": out, "holdings": holdings})

    for investor in investors:
        transactions = repository.load_transactions(
            connection, investor_id=investor["id"], date_to=on
        )
        client = {
            "id": investor["id"],
            "code": investor["code"],
            "display_name": investor["display_name"],
            "family_group_id": (
                str(investor["family_group_id"]) if investor["family_group_id"] else None
            ),
            "accounts": [],
        }
        unit(client, transactions)
        for account in repository.list_accounts(connection, investor["id"]):
            row = {
                "id": account["id"],
                "broker": account["broker"],
                "label": account["label"],
                "is_advised": account["is_advised"],
            }
            unit(row, [t for t in transactions if str(t.account_id) == account["id"]])
            client["accounts"].append(row)
        clients.append(client)

    prices = resolve_current_prices(connection, sorted(held), on).prices
    _figures(units, prices, on)
    return {
        "as_of": on,
        "families": families.list_families(connection, user_id),
        "clients": clients,
    }


@router.post("", response_model=InvestorOut, status_code=status.HTTP_201_CREATED)
def onboard_investor(
    body: InvestorIn,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if body.family_group_id:
        # Only one of this login's own families: any other id is "not found", never
        # silently attached (the column is a real reference since migration 079).
        try:
            families.assert_owned(connection, str(current_user["id"]), body.family_group_id)
        except families.FamilyNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such family.") from None
    if not (body.code or "").strip():
        return repository.create_investor_with_generated_code(
            connection,
            display_name=body.display_name,
            owner_user_id=str(current_user["id"]),
            family_group_id=body.family_group_id,
            pan=body.pan,
            onboarding_date=body.onboarding_date,
        )
    return create_investor(
        connection,
        code=body.code.strip(),
        display_name=body.display_name,
        family_group_id=body.family_group_id,
        pan=body.pan,
        onboarding_date=body.onboarding_date,
        owner_user_id=str(current_user["id"]),
        actor_uid=str(current_user["id"]),
    )


@router.get(
    "/{investor_id}/accounts",
    response_model=list[AccountOut],
    dependencies=[Depends(require_owned_investor)],
)
def list_accounts(
    investor_id: str,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """The investor's broker accounts -- what the Import screen's account selector
    offers, and what makes an already-onboarded account re-fetchable (until now the
    POST response was the only place an account ever appeared, so clients cached it in
    localStorage). Same shape as the POST response, forwarding addresses included."""
    code = repository.get_investor_code(connection, investor_id)
    if code is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such investor: {investor_id}")
    accounts = repository.list_accounts(connection, investor_id)
    for account in accounts:
        account["forwarding_addresses"] = forwarding_addresses(
            settings.forwarding_domain, code, account["broker"]
        )
        account["investor_id"] = investor_id
    return accounts


@router.get(
    "/{investor_id}/forwarding-code",
    response_model=list[ForwardingCodeOut],
    dependencies=[Depends(require_owned_investor)],
)
def forwarding_codes(
    investor_id: str,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    """Gmail forwarding verification codes caught for this investor, newest per address.

    Gmail mails a numeric code to a forwarding address and refuses to forward anything
    until that code is pasted back into Gmail's settings. The inbound webhook catches it
    (app/ingest/webhook.py) and this hands it back to the only person who can use it.

    An empty list is the normal answer almost all of the time -- before a filter has been
    created, and after the code has aged out -- so it is not a 404. Codes older than
    ``MAX_CODE_AGE_HOURS`` are omitted rather than shown stale: sending someone to paste
    a value Gmail has already forgotten is worse than showing nothing.

    404 when email forwarding is switched off (EMAIL_FORWARDING_ENABLED).
    """
    if not settings.email_forwarding_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email forwarding is turned off.")
    return forwarding_confirmation.latest_confirmation_codes(connection, investor_id)


@router.get(
    "/{investor_id}/onboarding",
    response_model=OnboardingStateOut,
    dependencies=[Depends(require_owned_investor)],
)
def onboarding_state(
    investor_id: str,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """How far along this investor's setup is -- derived on read, storing nothing.

    THERE IS NO ``setup_complete`` COLUMN, and adding one would be the bug. Every fact
    below is already in the database for its own reasons, so completeness is a question
    the ledger can answer rather than a flag something has to remember to set. That is
    CLAUDE.md's one architectural rule applied to onboarding, and it is what makes the
    answer identical on a laptop, a phone, and a browser whose storage was cleared.

    WHAT "COMPLETE" MEANS, and what it deliberately excludes. Consent, an account, and at
    least one transaction. Email forwarding is NOT part of it: plenty of people will
    upload a file once a quarter and never touch forwarding, and that is a finished setup
    rather than an abandoned one (docs/ONBOARDING_AS_IS_VS_TO_BE.md section 6). Counting
    it would make declining an optional feature read as failing setup.

    The one thing NOT derived here is "I'll do this later" -- that is a per-browser
    preference about being interrupted, not a fact about the account, and it stays in the
    client's own storage where it belongs.
    """
    if not repository.investor_exists(connection, investor_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such investor: {investor_id}")

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM account WHERE investor_id = %s", (investor_id,))
        accounts = int(cur.fetchone()[0])
        # Through account, because that is how the ledger is scoped -- ``transaction``
        # has no investor_id column and must not grow one: an investor owns accounts,
        # accounts own trades, and duplicating the link would be a second thing to keep
        # true. Counted rather than fetched: this endpoint answers "is there anything
        # here", and a 324-row ledger should not cross the wire to say "yes".
        cur.execute(
            """
            SELECT count(*) FROM transaction t
            JOIN account a ON a.id = t.account_id
            WHERE a.investor_id = %s
            """,
            (investor_id,),
        )
        transactions = int(cur.fetchone()[0])
        cur.execute("SELECT tos_version FROM app_user WHERE id = %s", (current_user["id"],))
        row = cur.fetchone()

    has_consent = consent.has_live_consent(connection, investor_id)
    complete = has_consent and accounts > 0 and transactions > 0

    # The first thing that is not done, so the wizard resumes where the person stopped
    # instead of walking them back through screens they already finished. Ordered by
    # dependency, not by preference: nothing can be uploaded without consent, and nothing
    # has anywhere to land without an account.
    if not has_consent:
        next_step = "permission"
    elif accounts == 0:
        next_step = "brokers"
    elif transactions == 0:
        next_step = "history"
    else:
        next_step = None

    return {
        "terms_accepted": bool(row and row[0]),
        "consent": has_consent,
        "accounts": accounts,
        "transactions": transactions,
        "forwarding_available": forwarding_enabled(settings.forwarding_domain),
        "complete": complete,
        "next_step": next_step,
    }


@router.put(
    "/{investor_id}/pan",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owned_investor)],
)
def set_pan(
    investor_id: str,
    body: SetPanIn,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Set or replace this investor's PAN after onboarding.

    ``POST /investors`` only ever accepted a PAN at creation time, so an investor
    onboarded before this system needed one had no way to add it afterwards -- the
    Zerodha/Groww/Upstox contract-note password lookup depends on it being on file.
    Same encryption path as onboarding (app.db.crypto), same investor_secret row.
    Never echoed back -- 204, no body, matching InvestorOut's write-only PAN.
    """
    set_investor_pan(connection, investor_id, body.pan, actor_uid=str(current_user["id"]))


@router.post(
    "/{investor_id}/accounts",
    response_model=AccountOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owned_investor)],
)
def onboard_account(
    investor_id: str,
    body: AccountIn,
    connection: Any = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    account = create_account(
        connection,
        investor_id=investor_id,
        broker=body.broker,
        dp_id=body.dp_id,
        client_id=body.client_id,
        is_advised=body.is_advised,
        label=body.label,
    )
    investor_code = account.pop("investor_code")
    account["forwarding_addresses"] = forwarding_addresses(
        settings.forwarding_domain, investor_code, account["broker"]
    )
    return account
