"""Per-IP rate limiting for the handful of endpoints an unauthenticated caller can hit
repeatedly: login, register, refresh. Not a defence against a determined attacker with a
botnet -- it exists to stop the cheap case, a script trying passwords or registering spam
accounts against a small alpha deployment with no other layer in front of it.

One shared ``Limiter`` so every limited route counts against the same per-IP buckets;
``app.main`` wires it into the FastAPI app and registers the 429 handler.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
