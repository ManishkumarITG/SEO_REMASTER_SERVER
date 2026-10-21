"""Persistence for Shopify connections, in the `shopify_connections` table.

Tokens go in encrypted and come out only through `access_token` /
`refresh_token`, so no caller handles ciphertext and no row is ever serialized
with a token in it. Every lookup that starts from a request is scoped to the
session's user id: a connection id from another session reads as "not found".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from db.session import async_session_factory, ensure_user
from exceptions.shopify import ShopifyNotConnected
from integrations.shopify.crypto import decrypt_token, encrypt_token
from integrations.shopify.oauth import TokenSet
from models.db import ShopifyConnection

ACTIVE = "active"
REAUTHORIZE_REQUIRED = "reauthorize_required"
REVOKED = "revoked"


@dataclass(slots=True)
class ShopInfo:
    """What the `shop` query says about the store, for display."""

    name: str = ""
    primary_domain: str = ""
    plan_name: str = ""
    currency_code: str = ""


def aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def access_token(row: ShopifyConnection) -> str:
    return decrypt_token(row.access_token_enc)


def refresh_token(row: ShopifyConnection) -> str | None:
    return decrypt_token(row.refresh_token_enc) if row.refresh_token_enc else None


def _apply_tokens(row: ShopifyConnection, tokens: TokenSet) -> None:
    row.access_token_enc = encrypt_token(tokens.access_token)
    row.access_token_expires_at = tokens.expires_at
    if tokens.refresh_token:
        row.refresh_token_enc = encrypt_token(tokens.refresh_token)
        row.refresh_token_expires_at = tokens.refresh_expires_at
    elif tokens.expires_at is None:
        # A non-expiring token replaces an expiring one: nothing to refresh.
        row.refresh_token_enc = None
        row.refresh_token_expires_at = None
    if tokens.scopes:
        row.scopes = ",".join(tokens.scopes)


def _apply_info(row: ShopifyConnection, info: ShopInfo | None) -> None:
    if info is None:
        return
    row.shop_name = info.name[:255] or row.shop_name
    row.primary_domain = info.primary_domain[:255] or row.primary_domain
    row.plan_name = info.plan_name[:64] or row.plan_name
    row.currency_code = info.currency_code[:8] or row.currency_code


async def save_connection(user_id: str, shop: str, tokens: TokenSet, info: ShopInfo | None) -> ShopifyConnection:
    """Insert or replace the connection for (user, shop). Reconnecting lands here
    too, so a new authorization always resets the status to active."""
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        await ensure_user(session, user_id)
        row = (await session.execute(
            select(ShopifyConnection).where(
                ShopifyConnection.user_id == user_id, ShopifyConnection.shop_domain == shop
            )
        )).scalar_one_or_none()
        if row is None:
            row = ShopifyConnection(user_id=user_id, shop_domain=shop, connected_at=now)
            session.add(row)
        else:
            row.connected_at = now
        _apply_tokens(row, tokens)
        _apply_info(row, info)
        row.status = ACTIVE
        row.status_reason = None
        row.last_refreshed_at = None
        row.last_checked_at = now if info is not None else None
        row.updated_at = now
        await session.commit()
        return row


async def list_connections(user_id: str) -> list[ShopifyConnection]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(ShopifyConnection)
            .where(ShopifyConnection.user_id == user_id)
            .order_by(ShopifyConnection.connected_at.desc())
        )
        return list(result.scalars())


async def get_connection(user_id: str | None, connection_id: str) -> ShopifyConnection:
    """The connection, if it belongs to this session. Else ShopifyNotConnected."""
    if not user_id:
        raise ShopifyNotConnected("No Shopify store is connected in this session")
    async with async_session_factory() as session:
        row = await session.get(ShopifyConnection, connection_id)
    if row is None or row.user_id != user_id:
        raise ShopifyNotConnected("That Shopify connection does not exist in this session")
    return row


async def find_connection(user_id: str, shop: str) -> ShopifyConnection | None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(ShopifyConnection).where(
                ShopifyConnection.user_id == user_id, ShopifyConnection.shop_domain == shop
            )
        )
        return result.scalar_one_or_none()


async def load_connection(connection_id: str) -> ShopifyConnection | None:
    """By id alone - for the token refresh, which re-reads the latest row."""
    async with async_session_factory() as session:
        return await session.get(ShopifyConnection, connection_id)


async def store_refreshed_tokens(connection_id: str, tokens: TokenSet) -> ShopifyConnection | None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        row = await session.get(ShopifyConnection, connection_id)
        if row is None:
            return None
        _apply_tokens(row, tokens)
        row.last_refreshed_at = now
        row.updated_at = now
        await session.commit()
        return row


async def set_status(connection_id: str, status: str, reason: str | None = None) -> None:
    async with async_session_factory() as session:
        row = await session.get(ShopifyConnection, connection_id)
        if row is None:
            return
        row.status = status
        row.status_reason = reason
        row.updated_at = datetime.now(UTC)
        await session.commit()


async def record_check(connection_id: str, info: ShopInfo) -> ShopifyConnection | None:
    """A request with the token just worked: refresh the display fields and mark active."""
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        row = await session.get(ShopifyConnection, connection_id)
        if row is None:
            return None
        _apply_info(row, info)
        row.status = ACTIVE
        row.status_reason = None
        row.last_checked_at = now
        row.updated_at = now
        await session.commit()
        return row


async def delete_connection(user_id: str | None, connection_id: str) -> bool:
    if not user_id:
        return False
    async with async_session_factory() as session:
        row = await session.get(ShopifyConnection, connection_id)
        if row is None or row.user_id != user_id:
            return False
        await session.delete(row)
        await session.commit()
        return True
