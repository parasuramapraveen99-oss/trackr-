"""A private cookie jar per NSE feed instance, shared with its warm-up request."""

import urllib.request

from app.exchange_reference import ReferenceOpener
from app.prices.sources import _ssl_context


def cookie_session() -> ReferenceOpener:
    # Installing a global opener would share cookies between unrelated feed runs.
    return ReferenceOpener(
        urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context()),
            urllib.request.HTTPCookieProcessor(),
        )
    )
