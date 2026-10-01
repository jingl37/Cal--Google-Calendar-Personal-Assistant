"""Encrypted per-user Google OAuth credential storage."""

import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from google.oauth2.credentials import Credentials

from supabase_store import AuthUser, admin_rest


SCOPES = ["https://www.googleapis.com/auth/calendar"]
# Google access tokens last an hour; assume that when the provider omits it.
DEFAULT_TOKEN_LIFETIME = 3600


def _cipher() -> Fernet:
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="Token encryption is not configured.")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Token encryption key is invalid.") from exc


def _encrypt(value: str | None) -> str | None:
    return _cipher().encrypt(value.encode()).decode() if value else None


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(status_code=503, detail="Stored Google credentials cannot be decrypted.") from exc


async def save_google_tokens(user: AuthUser, access_token: str, refresh_token: str | None, expires_in: int | None):
    existing = await get_google_connection(user.id)
    encrypted_refresh = _encrypt(refresh_token) if refresh_token else (existing or {}).get("encrypted_refresh_token")
    # Always record an expiry. A null expiry makes google-auth report the
    # credentials as valid forever, so the token would never be refreshed.
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in or DEFAULT_TOKEN_LIFETIME)
    payload = {
        "user_id": user.id,
        "google_account_email": user.email,
        "encrypted_access_token": _encrypt(access_token),
        "encrypted_refresh_token": encrypted_refresh,
        "token_expiry": expiry.isoformat(),
        "scopes": SCOPES,
    }
    rows = await admin_rest(
        "POST", "google_connections", payload=payload, params={"on_conflict": "user_id"},
        prefer="resolution=merge-duplicates,return=representation",
    )
    return rows[0]


async def get_google_connection(user_id: str):
    rows = await admin_rest("GET", "google_connections", params={"user_id": f"eq.{user_id}", "select": "*"})
    return rows[0] if rows else None


def connection_is_usable(row) -> bool:
    """Report whether a stored connection can still produce a live access token.

    A row on its own is not enough: once the access token expires, only a
    refresh token can revive it. Treating a dead row as "connected" hides the
    reconnect button and leaves the user with no way out.
    """
    if not row:
        return False
    if row.get("encrypted_refresh_token"):
        return True
    expiry = row.get("token_expiry")
    return bool(expiry) and datetime.fromisoformat(expiry) > datetime.now(timezone.utc)


async def delete_google_connection(user_id: str):
    await admin_rest("DELETE", "google_connections", params={"user_id": f"eq.{user_id}"})


async def save_refreshed_credentials(user: AuthUser, credentials: Credentials) -> None:
    """Persist a freshly refreshed access token using its real lifetime."""
    expires_in = None
    if credentials.expiry:
        expires_in = int((credentials.expiry.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds())
    await save_google_tokens(user, credentials.token, credentials.refresh_token, max(expires_in or 0, 60))


async def google_credentials(user: AuthUser) -> Credentials:
    row = await get_google_connection(user.id)
    if not row:
        raise HTTPException(status_code=409, detail="Connect Google Calendar to continue.")
    # google-auth compares expiry with a naive UTC datetime internally. A row
    # with no expiry predates this fix, so treat it as stale and force a refresh.
    if row.get("token_expiry"):
        expiry = datetime.fromisoformat(row["token_expiry"]).astimezone(timezone.utc).replace(tzinfo=None)
    else:
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    return Credentials(
        token=_decrypt(row.get("encrypted_access_token")),
        refresh_token=_decrypt(row.get("encrypted_refresh_token")),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        scopes=SCOPES,
        expiry=expiry,
    )
