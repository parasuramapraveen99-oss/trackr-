"""Field-level encryption and PAN hashing.

Two secrets, deliberately separate:

  PORTFOLIO_ENCRYPTION_KEY  encrypts PAN and demat identifiers at rest
  PORTFOLIO_PAN_PEPPER      salts the PAN lookup hash

Separate so that leaking one does not defeat the other. If they were the same value, an
attacker who obtained it could both decrypt the stored PANs and confirm guesses against
the hashes; splitting them means a partial compromise stays partial.

Neither has a default. A missing key RAISES rather than falling back to plaintext -- the
same philosophy as ``MissingPriceError`` in the engine. Silent degradation is how a
column of what everyone believes is ciphertext turns out to be readable.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

ENCRYPTION_KEY_ENV = "PORTFOLIO_ENCRYPTION_KEY"
PAN_PEPPER_ENV = "PORTFOLIO_PAN_PEPPER"

#: Five letters, four digits, one letter. The whole space is enumerable in minutes, which
#: is exactly why the lookup hash is peppered rather than a bare digest.
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


class EncryptionNotConfiguredError(RuntimeError):
    """A required secret is absent. Never degrade to plaintext."""


class InvalidPANError(ValueError):
    """PAN does not match the mandated format."""


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get(ENCRYPTION_KEY_ENV)
    if not key:
        raise EncryptionNotConfiguredError(
            f"{ENCRYPTION_KEY_ENV} is not set. Refusing to store identifiers in "
            "plaintext. Generate one with Fernet.generate_key()."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def _pepper() -> bytes:
    pepper = os.environ.get(PAN_PEPPER_ENV)
    if not pepper:
        raise EncryptionNotConfiguredError(
            f"{PAN_PEPPER_ENV} is not set. An unpeppered PAN hash is reversible by brute "
            "force: the format is fixed and the whole space is enumerable in minutes."
        )
    return pepper.encode()


def generate_key() -> str:
    """A fresh encryption key. For setup and tests; never call this at request time."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def encrypt(plaintext: str) -> bytes:
    """Authenticated encryption. Tampered ciphertext fails to decrypt rather than
    returning garbage that looks like data."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes) -> str:
    """Decrypt. Raises ``cryptography.fernet.InvalidToken`` on a wrong key or tampering."""
    return _fernet().decrypt(bytes(ciphertext)).decode()


def normalise_pan(pan: str) -> str:
    """Uppercase and strip. Broker documents are inconsistent about both."""
    cleaned = pan.strip().upper()
    if not PAN_PATTERN.match(cleaned):
        raise InvalidPANError(f"not a valid PAN: {redact_pan(cleaned)}")
    return cleaned


def hash_pan(pan: str) -> str:
    """Stable, peppered lookup hash. Same PAN in, same hash out, forever.

    Lets a PAN be found without decrypting anything. That is its ONLY purpose -- it is
    not a password hash and must never be treated as one.
    """
    return hmac.new(_pepper(), normalise_pan(pan).encode(), hashlib.sha256).hexdigest()


def redact_pan(pan: str) -> str:
    """Mask for logs, errors and stack traces.

    ``ABCDE1234F`` -> ``ABXXXXXX4F``. Enough to correlate two mentions of the same PAN in
    a log; not enough to be one. Every error path in this module goes through here,
    because a PAN in a traceback is a disclosure that no amount of encryption at rest
    undoes.
    """
    if len(pan) < 4:
        return "X" * len(pan)
    return f"{pan[:2]}{'X' * (len(pan) - 4)}{pan[-2:]}"
