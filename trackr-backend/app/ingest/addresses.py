"""Per-account inbound-forwarding addresses -- app/routers/investors.py surfaces these
on ``AccountOut`` so a client has something to paste into a Gmail filter instead of
docs/INGESTION.md prose nobody can act on. See docs/INGESTION.md "Adapter 3 -- Gmail
forwarding".

Derived, never stored: CLAUDE.md's "everything is derived" discipline applies here too
-- INGEST_DOMAIN + the investor's code + the account's broker is enough to reconstruct
every address on every read, so no address is ever written to a row.

NO ADDRESS IS BETTER THAN A DEAD ONE. Two conditions have to hold before an address is
worth showing, and this module refuses on either: the broker needs a real adapter, and
the deployment needs a domain that can actually receive mail. Until this gate existed,
a development deployment cheerfully handed every user
``praveen-a3f2-zerodha-contractnote@ingest.example.com`` -- an RFC 2606 reserved name that
by definition can never receive anything -- alongside four steps telling them to paste it
into Gmail (docs/ONBOARDING_AS_IS_VS_TO_BE.md section 8, decision 2).

Gating HERE, in the one place addresses are derived, rather than behind a frontend flag,
is what makes the whole feature appear and disappear as one piece: the wizard's forwarding
step, the Settings panel and the broker picker's "email ✅" badge all read from this, so
none of them can be switched on independently and none has to remember to check. The day a
real INGEST_DOMAIN is configured, every one of them lights up on its own.
"""

from __future__ import annotations

from app.ingest.webhook import _ADAPTER_FACTORIES

#: Domain suffixes that cannot receive mail, ever, by standards rather than by policy:
#: RFC 2606's reserved TLDs and second-level names, plus the loopback name. A deployment
#: pointing at one of these is a deployment with no inbound mail -- which is the ordinary
#: state during development and must not be dressed up as a working address.
#:
#: Checked as a suffix set rather than against ``app.config``'s single dev default so that
#: ANY reserved name is caught, not just the one that happens to be the default today.
_UNROUTABLE_SUFFIXES = (
    ".example.com",
    ".example.net",
    ".example.org",
    ".example",
    ".invalid",
    ".test",
    ".localhost",
)


def forwarding_enabled(domain: str) -> bool:
    """Can this deployment receive mail at all?

    False for an unset domain and for every RFC 2606 reserved name. Callers use it to
    decide whether inbound forwarding exists as a feature on this deployment -- the
    per-broker question is ``broker_has_forwarding`` below.
    """
    name = (domain or "").strip().lower().rstrip(".")
    if not name or "." not in name:
        return False
    return not any(
        name == suffix.lstrip(".") or name.endswith(suffix)
        for suffix in _UNROUTABLE_SUFFIXES
    )


def broker_has_email_adapter(broker: str) -> bool:
    """Can we parse this broker's email at all, on any deployment?

    Deliberately domain-independent, and that is the whole reason it exists separately
    from ``broker_has_forwarding``. Both halves of that AND are false at once on a
    deployment with no mail domain, so a caller holding only ``forwarding`` cannot tell
    "we have never written a parser for Groww" from "this deployment cannot receive mail
    yet" -- and a UI that cannot tell them apart has to hedge between the two in a
    sentence that is half wrong whichever broker it names.
    """
    broker_key = broker.strip().lower()
    return any(registered == broker_key for registered, _doctype in _ADAPTER_FACTORIES)


def broker_has_forwarding(domain: str, broker: str) -> bool:
    """True when this broker on this deployment would actually yield an address.

    The exact condition ``forwarding_addresses`` applies, without needing an investor code
    to ask it -- the broker picker has to answer "email ✅ or email —" before any account
    exists.
    """
    return forwarding_enabled(domain) and broker_has_email_adapter(broker)


def forwarding_addresses(domain: str, investor_code: str, broker: str) -> list[str]:
    """One address per ``(broker, doctype)`` pair with a real adapter in
    ``app.ingest.webhook._ADAPTER_FACTORIES`` -- read from that registry rather than a
    separately hand-listed set of doctypes, so the two cannot drift apart.

    Empty for two distinct reasons, both of which mean "do not show an address":

    * the broker has no adapter yet (or is misspelled) -- CLAUDE.md: never invent parsing
      logic for a source the codebase doesn't handle, so this never fabricates an address
      for a doctype nothing here can parse;
    * the deployment has no mail-receiving domain -- see ``forwarding_enabled``.

    The caller does not need to tell the two apart. Both answers are "there is no address
    for this", and a UI that has none is expected to say so in words rather than render a
    blank field (app/components/EmailForwardingSetup.tsx already does).
    """
    if not forwarding_enabled(domain):
        return []
    code = investor_code.strip().lower()
    broker_key = broker.strip().lower()
    return sorted(
        f"{code}-{registered_broker}-{doctype}@{domain}"
        for registered_broker, doctype in _ADAPTER_FACTORIES
        if registered_broker == broker_key
    )
