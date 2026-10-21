"""Encryption at rest for Shopify access and refresh tokens.

A refresh token mints new access tokens for 90 days without the merchant, so a
copy of seo.db must not be a copy of every connected store's admin access.
Tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, authenticated),
keyed from SHOPIFY_TOKEN_ENCRYPTION_KEY, or SESSION_SECRET when that is blank.

The configured secret is stretched with HKDF rather than used directly, so any
long random string works as the setting and the derived key is bound to this one
purpose: the same SESSION_SECRET that signs cookies never doubles as this key.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config import get_settings
from exceptions.shopify import ShopifyNotConfigured, ShopifyReauthorizeRequired

_HKDF_INFO = b"seo-remaster/shopify-token/v1"


@lru_cache(maxsize=4)
def _fernet_for(secret: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def _fernet() -> Fernet:
    settings = get_settings()
    secret = settings.shopify_token_encryption_key or settings.session_secret
    if not secret:
        raise ShopifyNotConfigured(
            "Shopify tokens cannot be stored safely: set SHOPIFY_TOKEN_ENCRYPTION_KEY "
            "(or SESSION_SECRET) in server/.env"
        )
    return _fernet_for(secret)


def encryption_ready() -> bool:
    settings = get_settings()
    return bool(settings.shopify_token_encryption_key or settings.session_secret)


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """-> the token. A key change makes old ciphertext unreadable: reconnect."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ShopifyReauthorizeRequired(
            "The stored Shopify credentials cannot be read because the encryption key "
            "changed. Reconnect the store."
        ) from exc
