"""A private cookie jar per NSE feed instance, shared with its warm-up request."""

import urllib.request

from app.prices.sources import _ssl_context


def cookie_session() -> urllib.request.OpenerDirector:
    # Installing a global opener would share cookies between unrelated feed runs.
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        urllib.request.HTTPCookieProcessor(),
    )
