"""Authenticated Supabase access for Calendar Assistant.

Browser requests carry a Supabase access token. Database calls reuse that token,
so PostgreSQL row-level security remains the final ownership check.
"""

import os
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    token: str


def _configured() -> None:
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        raise HTTPException(status_code=503, detail="Supabase is not configured on the backend.")


async def current_user(authorization: str | None = Header(default=None)) -> AuthUser:
    """Validate a Supabase session and return the authenticated identity."""
    _configured()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    token = authorization.removeprefix("Bearer ").strip()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_PUBLISHABLE_KEY, "Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Your session expired. Sign in again.")
    payload = response.json()
    return AuthUser(id=payload["id"], email=payload.get("email") or "", token=token)


async def rest(user: AuthUser, method: str, table: str, *, params=None, payload=None, prefer=None):
    """Call Supabase Data API as the user so RLS policies always apply."""
    headers = {
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {user.token}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(
            method,
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="The private data store rejected the request.")
    if not response.content:
        return None
    return response.json()


async def admin_rest(method: str, table: str, *, params=None, payload=None, prefer=None):
    """Backend-only Data API access for encrypted OAuth credentials."""
    if not SUPABASE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Backend secret key is not configured.")
    headers = {"apikey": SUPABASE_SECRET_KEY, "Content-Type": "application/json"}
    # Legacy service-role keys are JWTs and also require the Bearer header.
    if SUPABASE_SECRET_KEY.count(".") == 2:
        headers["Authorization"] = f"Bearer {SUPABASE_SECRET_KEY}"
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(
            method,
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Secure connection storage rejected the request.")
    if not response.content:
        return None
    return response.json()


async def get_profile(user: AuthUser) -> dict:
    rows = await rest(user, "GET", "profiles", params={"user_id": f"eq.{user.id}", "select": "*"})
    if rows:
        return rows[0]
    metadata_name = user.email.split("@", 1)[0] if user.email else ""
    rows = await rest(
        user,
        "POST",
        "profiles",
        payload={"user_id": user.id, "display_name": metadata_name},
        prefer="return=representation",
    )
    return rows[0]


async def save_profile(user: AuthUser, values: dict) -> dict:
    allowed = {key: values[key] for key in ("display_name", "timezone", "role", "facts") if key in values}
    rows = await rest(
        user,
        "POST",
        "profiles",
        payload={"user_id": user.id, **allowed},
        params={"on_conflict": "user_id"},
        prefer="resolution=merge-duplicates,return=representation",
    )
    return rows[0]


async def list_conversations(user: AuthUser) -> list[dict]:
    return await rest(
        user,
        "GET",
        "conversations",
        params={"user_id": f"eq.{user.id}", "select": "*", "order": "updated_at.desc", "limit": "12"},
    )


async def get_conversation(user: AuthUser, conversation_id: str) -> dict | None:
    rows = await rest(
        user,
        "GET",
        "conversations",
        params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user.id}", "select": "*"},
    )
    return rows[0] if rows else None


async def create_conversation(user: AuthUser, title: str = "New conversation") -> dict:
    rows = await rest(
        user,
        "POST",
        "conversations",
        payload={"user_id": user.id, "title": title, "messages": []},
        prefer="return=representation",
    )
    return rows[0]


async def save_conversation(user: AuthUser, conversation: dict) -> dict:
    rows = await rest(
        user,
        "PATCH",
        "conversations",
        params={"id": f"eq.{conversation['id']}", "user_id": f"eq.{user.id}"},
        payload={
            "title": conversation["title"],
            "messages": conversation.get("messages", []),
            "pending_schedule": conversation.get("pending_schedule"),
        },
        prefer="return=representation",
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return rows[0]
