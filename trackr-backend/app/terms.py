"""The version string for the Terms of Service + Privacy Policy a login accepts.

ONE CONSTANT, TWO READERS. The registration handler stamps this onto ``app_user
.tos_version``; the frontend's ``/terms`` page renders the matching text and sends this
same string back with the registration. They must agree, which is why the value lives
here rather than being typed into both -- a mismatch would record an acceptance of a
version nobody was ever shown, which is worse than recording nothing.

DISTINCT FROM ``app.db.consent.CURRENT_NOTICE_VERSION``. That one versions the DPDP
processing notice, which belongs to an INVESTOR and is withdrawable. This one versions
the service contract, which belongs to the LOGIN and is not. docs/
ONBOARDING_AS_IS_VS_TO_BE.md section 7 has the full distinction; they are deliberately
separate records with separate lifecycles, and collapsing them into one would make both
wrong.

BUMPING IT. Change this string whenever the terms text changes in a way a person would
want to be asked about again. Existing rows keep the version they accepted -- that is
the entire point of storing a version rather than a boolean.
"""

from __future__ import annotations

#: Bump together with the text on the frontend's /terms page.
CURRENT_TOS_VERSION = "2026-10-terms-v2"
