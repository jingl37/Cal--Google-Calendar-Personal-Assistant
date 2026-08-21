"""Encrypted per-user Google OAuth credential storage."""

import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from google.oauth2.credentials import Credentials

from supabase_store import AuthUser, admin_rest


SCOPES = ["https://www.googleapis.com/auth/calendar"]


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
    expiry = None
    if expires_in:
        expiry = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + expires_in, tz=timezone.utc).isoformat()
    payload = {
        "user_id": user.id,
        "google_account_email": user.email,
        "encrypted_access_token": _encrypt(access_token),
        "encrypted_refresh_token": encrypted_refresh,
        "token_expiry": expiry,
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


async def delete_google_connection(user_id: str):
    await admin_rest("DELETE", "google_connections", params={"user_id": f"eq.{user_id}"})


async def google_credentials(user: AuthUser) -> Credentials:
    row = await get_google_connection(user.id)
    if not row:
        raise HTTPException(status_code=409, detail="Connect Google Calendar to continue.")
    expiry = None
    if row.get("token_expiry"):
        # google-auth compares expiry with a naive UTC datetime internally.
        expiry = datetime.fromisoformat(row["token_expiry"]).astimezone(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token=_decrypt(row.get("encrypted_access_token")),
        refresh_token=_decrypt(row.get("encrypted_refresh_token")),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        scopes=SCOPES,
        expiry=expiry,
    )
