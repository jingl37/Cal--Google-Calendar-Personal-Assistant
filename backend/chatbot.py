import json
import os
import base64
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from google.genai.errors import APIError
from google.auth.transport.requests import Request as GoogleAuthRequest
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from calendar_assist import (
    create_new_calendar_event,
    delete_calendar_event,
    get_calendar_event,
    get_calendar_free_slot,
    get_calendar_service,
    is_calendar_slot_free,
    list_events_in_window,
    update_calendar_event,
    set_calendar_credentials,
)

# Load the shared .env before importing modules that read settings at import time.
BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR.parent / ".env")

from supabase_store import (
    AuthUser,
    create_conversation as db_create_conversation,
    current_user,
    get_conversation as db_get_conversation,
    get_profile as db_get_profile,
    list_conversations as db_list_conversations,
    save_conversation as db_save_conversation,
    save_profile as db_save_profile,
)
from google_connections import (
    connection_is_usable,
    delete_google_connection,
    get_google_connection,
    google_credentials,
    save_google_tokens,
    save_refreshed_credentials,
)

app = FastAPI(title="Cal API")
api_key = os.getenv("API_KEY")
if not api_key:
    raise ValueError("API_KEY not found — check your .env file")

# A pinned version eventually stops being offered to new API keys, which looks
# like a broken app. The alias tracks Google's current Flash model instead.
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
# The full Flash models shed load with 503s and burn free-tier quota with 429s.
# The Lite tier stays available, so fall back to it rather than failing a chat.
MODEL_CHAIN = list(dict.fromkeys([MODEL, "gemini-flash-lite-latest", "gemini-3.1-flash-lite"]))

# Gemini receives these functions as tools so it can take calendar actions when needed.
client = genai.Client(api_key=api_key)
# Remembered facts for the user behind the current request. Calendar tools run
# deep inside the Gemini SDK, so a context variable is how they reach them.
USER_FACTS = ContextVar("user_profile_facts", default=())
# Limit uploads to common schedule formats and a safe file size.
SUPPORTED_SCHEDULE_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_SCHEDULE_SIZE = 15 * 1024 * 1024
MAX_SAVED_PREVIEW_SIZE = 3 * 1024 * 1024

# Let the local Vite app and the deployed frontend call this API.
frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
allowed_origins = [
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
if frontend_url:
    allowed_origins.append(frontend_url)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatInput(BaseModel):
    message: str
    conversation_id: str | None = None
    timezone: str = "America/Toronto"
    name: str = "there"


class ConversationInput(BaseModel):
    name: str = "there"


class ScheduleClass(BaseModel):
    name: str
    days: list[str]
    start_time: str
    end_time: str
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class ScheduleReviewInput(BaseModel):
    classes: list[ScheduleClass]

class EventInput(BaseModel):
    title: str
    start: str
    end: str
    location: str = ""
    description: str = ""


class GoogleTokenInput(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None


async def calendar_user(user: AuthUser = Depends(current_user)) -> AuthUser:
    """Bind the signed-in user's stored Google credentials to this request."""
    credentials = await google_credentials(user)
    if credentials.expired:
        if not credentials.refresh_token:
            raise HTTPException(status_code=409, detail="Reconnect Google Calendar to continue.")
        try:
            # Refreshing is a blocking HTTPS call, so keep it off the event loop.
            await run_in_threadpool(credentials.refresh, GoogleAuthRequest())
        except Exception as exc:
            # Google rejected the refresh token (revoked, or expired because the
            # OAuth consent screen is still in Testing). Drop the dead row so the
            # status endpoint stops advertising a connection that cannot work.
            await delete_google_connection(user.id)
            raise HTTPException(status_code=409, detail="Reconnect Google Calendar to continue.") from exc
        await save_refreshed_credentials(user, credentials)
    set_calendar_credentials(credentials)
    return user


WEEKDAYS = {
    "monday": (0, "MO"), "tuesday": (1, "TU"), "wednesday": (2, "WE"),
    "thursday": (3, "TH"), "friday": (4, "FR"), "saturday": (5, "SA"), "sunday": (6, "SU"),
}


class ProfileInput(BaseModel):
    role: str = ""
    display_name: str | None = None
    timezone: str | None = None

@app.get("/api/profile")
async def get_profile(user: AuthUser = Depends(current_user)):
    return await db_get_profile(user)

@app.put("/api/profile")
async def update_profile(input_data: ProfileInput, user: AuthUser = Depends(current_user)):
    return await db_save_profile(user, input_data.model_dump(exclude_none=True))


@app.get("/api/auth/google/status")
async def google_connection_status(user: AuthUser = Depends(current_user)):
    connection = await get_google_connection(user.id)
    # Report a dead connection as disconnected so the UI offers Connect again.
    return {"connected": connection_is_usable(connection), "email": connection.get("google_account_email") if connection else None}


@app.post("/api/auth/google/token")
async def store_google_connection(input_data: GoogleTokenInput, user: AuthUser = Depends(current_user)):
    await save_google_tokens(user, input_data.access_token, input_data.refresh_token, input_data.expires_in)
    return {"connected": True}


@app.delete("/api/auth/google/token")
async def remove_google_connection(user: AuthUser = Depends(current_user)):
    await delete_google_connection(user.id)
    return {"connected": False}


def conversation_title(message: str) -> str:
    """Use the first few words of a new chat as its sidebar title."""
    words = message.strip().split()
    return " ".join(words[:7]).rstrip(".,!? ") or "New conversation"


async def get_or_create_conversation(user, conversation_id, first_message="New conversation"):
    """Reuse an owned conversation or create one in the signed-in user's account."""
    conversation = await db_get_conversation(user, conversation_id) if conversation_id else None
    if conversation:
        return conversation
    return await db_create_conversation(user, conversation_title(first_message))


async def load_user_facts(user: AuthUser) -> dict:
    """Expose the signed-in user's remembered facts to the calendar guardrails."""
    profile = await db_get_profile(user)
    USER_FACTS.set(tuple(profile.get("facts") or ()))
    return profile


class GeminiUnavailable(Exception):
    """Every model in the chain was overloaded or rate limited."""


def call_gemini(make_call, attempts: int = 2, delay: float = 1.5):
    """Run a Gemini call, retrying overload and then falling back a tier.

    `make_call` takes a model name. Flash returns 503 UNAVAILABLE under load
    and 429 once free-tier quota is gone; both are transient for that model
    but can persist for minutes, so after a brief retry we move down the
    chain to a Lite model rather than failing the user's message.
    """
    last = None
    for model in MODEL_CHAIN:
        for attempt in range(attempts):
            try:
                return make_call(model)
            except APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if status not in (429, 503, 404):
                    raise
                last = exc
                if status != 404 and attempt < attempts - 1:
                    time.sleep(delay * (attempt + 1))
                if status == 404:
                    break
    raise GeminiUnavailable(str(last))


def clean_json(text):
    """Handle a model response whether it is plain JSON or wrapped in a code block."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def extract_schedule(file_bytes, mime_type, timezone):
    """Ask Gemini to turn an uploaded schedule into structured class details."""
    prompt = f"""Read this class schedule image or PDF. Extract classes accurately into JSON only—no markdown.
Return exactly this shape: {{"classes":[{{"name":"", "days":["Monday"], "start_time":"HH:MM", "end_time":"HH:MM", "location":"" or null, "start_date":"YYYY-MM-DD" or null, "end_date":"YYYY-MM-DD" or null}}], "missing":["..."], "notes":["..."]}}.
Use 24-hour time. Do not guess unreadable names, days, dates, locations, or term boundaries; place missing information in `missing` instead. The user's timezone is {timezone}."""
    response = call_gemini(lambda model: client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(temperature=0),
    ))
    # Only keep classes that have enough information to be reviewed safely.
    data = clean_json(response.text or "{}")
    classes = [item for item in data.get("classes", []) if item.get("name") and item.get("days") and item.get("start_time") and item.get("end_time")]
    classes = merge_duplicate_classes(classes)
    return {"classes": classes, "missing": data.get("missing", []), "notes": data.get("notes", [])}


def schedule_summary(schedule):
    """Format extracted classes as a readable message for the user to confirm."""
    classes = schedule["classes"]
    lines = [f"• {item['name']} — {', '.join(item['days'])}, {item['start_time']}–{item['end_time']}" + (f" ({item['location']})" if item.get("location") else "") for item in classes]
    return "\n".join(lines)


def merge_duplicate_classes(classes):
    """Merge repeated extractions of the same course meeting and combine its days."""
    merged = {}
    for item in classes:
        key = (
            item["name"].strip().lower(), item["start_time"], item["end_time"],
            (item.get("location") or "").strip().lower(), item.get("start_date"), item.get("end_date"),
        )
        if key not in merged:
            merged[key] = {**item, "days": list(dict.fromkeys(item["days"]))}
        else:
            merged[key]["days"] = list(dict.fromkeys(merged[key]["days"] + item["days"]))
    return list(merged.values())


def validate_schedule(classes):
    """Check dates, times, and weekdays before importing recurring events."""
    if not classes:
        raise HTTPException(status_code=422, detail="Add at least one class before importing.")
    for item in classes:
        if not item.get("start_date") or not item.get("end_date"):
            raise HTTPException(status_code=422, detail="Add the term start and end dates before importing.")
        try:
            start_date = datetime.fromisoformat(item["start_date"]).date()
            end_date = datetime.fromisoformat(item["end_date"]).date()
            datetime.strptime(item["start_time"], "%H:%M")
            datetime.strptime(item["end_time"], "%H:%M")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Use valid dates and 24-hour times for every class.") from exc
        if end_date < start_date or item["end_time"] <= item["start_time"]:
            raise HTTPException(status_code=422, detail="Check that every class ends after it starts and the term dates are in order.")
        if any(day.lower() not in WEEKDAYS for day in item["days"]):
            raise HTTPException(status_code=422, detail="Each class needs a valid weekday.")
        for day in item["days"]:
            target_day, _ = WEEKDAYS[day.lower()]
            if start_date + timedelta(days=(target_day - start_date.weekday()) % 7) > end_date:
                raise HTTPException(status_code=422, detail=f"{item['name']} has no {day} meeting within the selected term.")


def first_class_occurrence(item, weekday, timezone):
    """Find the first matching weekday in the supplied term."""
    start_date = datetime.fromisoformat(item["start_date"]).date()
    target_day, _ = WEEKDAYS[weekday.lower()]
    days_until = (target_day - start_date.weekday()) % 7
    date = start_date + timedelta(days=days_until)
    start_time = datetime.strptime(item["start_time"], "%H:%M").time()
    end_time = datetime.strptime(item["end_time"], "%H:%M").time()
    zone = ZoneInfo(timezone)
    return datetime.combine(date, start_time, tzinfo=zone), datetime.combine(date, end_time, tzinfo=zone)


def calendar_conflicts(start_text, end_text, timezone, location="", exclude_event_id=None, events=None):
    """Find overlaps and short transitions for a proposed calendar time.

    Callers that test several candidate times can pass a pre-fetched event
    list covering all of them, which avoids re-querying every calendar.
    """
    start, end = datetime.fromisoformat(start_text), datetime.fromisoformat(end_text)
    if start.tzinfo is None: start = start.replace(tzinfo=ZoneInfo(timezone))
    if end.tzinfo is None: end = end.replace(tzinfo=ZoneInfo(timezone))
    if events is None:
        events = calendar_events(start - timedelta(minutes=15), end + timedelta(minutes=15))
    overlaps, transitions = [], []
    for event in events:
        if event.get("id") == exclude_event_id: continue
        event_start = event.get("start", {}).get("dateTime"); event_end = event.get("end", {}).get("dateTime")
        if not event_start or not event_end: continue
        event_start, event_end = datetime.fromisoformat(event_start), datetime.fromisoformat(event_end)
        title, event_location = event.get("summary", "Another event"), event.get("location", "")
        if start < event_end and end > event_start: overlaps.append(f"{title} ({event_start.strftime('%-I:%M %p')}–{event_end.strftime('%-I:%M %p')})")
        else:
            gap = (start - event_end).total_seconds() / 60 if event_end <= start else (event_start - end).total_seconds() / 60
            if 0 <= gap < 15 and location and event_location and location.lower() != event_location.lower(): transitions.append(f"Only {int(gap)} minutes between this and {title} at {event_location}.")
    return overlaps, transitions


def preference_warnings(start_text, end_text):
    """Apply simple guardrails based on the user facts Cal has learned."""
    # Facts come from the per-request context variable, so one user's
    # preferences can never leak into another user's calendar checks.
    facts = " ".join(USER_FACTS.get()).lower()
    start, end = datetime.fromisoformat(start_text), datetime.fromisoformat(end_text)
    warnings = []
    if any(phrase in facts for phrase in ("avoid early", "not a morning", "hate mornings")) and start.hour < 9: warnings.append("This is before 9 AM, and you previously said you prefer to avoid early mornings.")
    if (end - start) > timedelta(hours=3): warnings.append("This is longer than three hours; consider adding a break.")
    return warnings


def suggest_open_times(start_text, end_text, timezone):
    """Offer nearby same-day alternatives with the same duration."""
    start = datetime.fromisoformat(start_text); end = datetime.fromisoformat(end_text); duration = end - start
    if start.tzinfo is None: start = start.replace(tzinfo=ZoneInfo(timezone))
    if end.tzinfo is None: end = end.replace(tzinfo=ZoneInfo(timezone))
    offsets = (0.5, 1, 1.5, 2, -0.5, -1)
    # One fetch spanning every candidate. Querying per candidate would hit each
    # of the user's calendars six times over.
    events = calendar_events(start + timedelta(hours=min(offsets)) - timedelta(minutes=15),
                             start + timedelta(hours=max(offsets)) + duration + timedelta(minutes=15))
    suggestions = []
    # Check the nearest useful slots first, including a 30-minute opening after a meeting.
    for offset in offsets:
        candidate = start + timedelta(hours=offset); candidate_end = candidate + duration
        if candidate.hour < 7 or candidate_end.hour > 22: continue
        if not calendar_conflicts(candidate.isoformat(), candidate_end.isoformat(), timezone, events=events)[0]: suggestions.append(candidate.strftime("%A at %-I:%M %p"))
    return suggestions[:2]


def safe_create_calendar_event(summary: str, start_time: str, end_time: str, time_zone: str, description: str = None, location: str = None, confirmed: bool = False):
    """Create an event only when it passes conflict and preference checks."""
    overlaps, transitions = calendar_conflicts(start_time, end_time, time_zone, location or "")
    warnings = preference_warnings(start_time, end_time)
    if overlaps and not confirmed:
        alternatives = suggest_open_times(start_time, end_time, time_zone)
        return json.dumps({"needs_confirmation": True, "reason": f"Overlaps with {', '.join(overlaps)}.", "alternatives": alternatives, "message": "Explain the overlap, offer the alternatives, and ask: 'Would you like me to add it anyway?' Do not create the event yet."})
    if (transitions or warnings) and not confirmed:
        return json.dumps({"needs_confirmation": True, "reason": " ".join(transitions + warnings), "message": "Explain this warning, offer any sensible alternative, and ask: 'Would you like me to keep this time anyway?' Do not create the event yet."})
    return create_new_calendar_event(summary, start_time, end_time, time_zone, description, location)


# Expose the guarded create function, rather than the raw create function, to Cal.
calendar_tools = [safe_create_calendar_event, delete_calendar_event, get_calendar_event, update_calendar_event, is_calendar_slot_free, get_calendar_free_slot]


def calendar_events(start: datetime, end: datetime):
    """Read events across every calendar the user keeps visible, not just primary."""
    try:
        return list_events_in_window(start.isoformat(), end.isoformat())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Calendar Error: {exc}") from exc


def serialize_event(event, timezone: str):
    """Reduce Google's event object to the fields the React timeline needs."""
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event["id"],
        # The timeline needs this to edit an event on a secondary calendar.
        "calendarId": event.get("calendarId", "primary"),
        "title": event.get("summary") or "Untitled event",
        "description": event.get("description") or "",
        "location": event.get("location") or "",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "allDay": "date" in start and "dateTime" not in start,
        "timezone": start.get("timeZone") or timezone,
    }


def build_suggestions(events, day: datetime, timezone: str):
    """Create useful Ask Cal prompts from the selected day's schedule."""
    dated_events = [event for event in events if not event.get("allDay")]
    suggestions = []
    if dated_events:
        suggestions.append(f"Review my {len(dated_events)} event{'s' if len(dated_events) != 1 else ''} for {day.strftime('%A')}")
        first = dated_events[0]["title"]
        suggestions.append(f"Help me prepare for {first}")
    else:
        suggestions.append(f"Help me plan {day.strftime('%A')}")
        suggestions.append("Find time for a workout this week")
    suggestions.append("What is one thing I should prioritize this week?")
    return suggestions[:3]


@app.get("/test")
async def test():
    # Small health-check route used to confirm the backend is running.
    return {"ok": True}


@app.get("/api/calendar")
async def get_calendar(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    timezone: str = "America/Toronto",
    user: AuthUser = Depends(calendar_user),
):
    try:
        # ZoneInfo raises ZoneInfoNotFoundError (a KeyError) for unknown zones.
        zone = ZoneInfo(timezone)
        day = datetime.fromisoformat(date).replace(tzinfo=zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=400, detail="Invalid date or timezone") from exc
    # Fetch one day's events, then pair them with contextual prompt suggestions.
    raw_events = await run_in_threadpool(calendar_events, day, day + timedelta(days=1))
    events = [serialize_event(event, timezone) for event in raw_events]
    return {"date": date, "events": events, "suggestions": build_suggestions(events, day, timezone)}

def apply_event_update(event_id: str, input_data: "EventInput", timezone: str, calendar_id: str):
    """Blocking half of the timeline editor save, run off the event loop."""
    overlaps, transitions = calendar_conflicts(input_data.start, input_data.end, timezone, input_data.location, event_id)
    warnings = preference_warnings(input_data.start, input_data.end)
    if overlaps or transitions or warnings:
        alternatives = suggest_open_times(input_data.start, input_data.end, timezone) if overlaps else []
        raise HTTPException(status_code=409, detail={"reason": " ".join(([f"Overlaps with {', '.join(overlaps)}."] if overlaps else []) + transitions + warnings), "alternatives": alternatives})
    service = get_calendar_service()
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    event.update({"summary": input_data.title, "location": input_data.location, "description": input_data.description,
      "start": {"dateTime": input_data.start, "timeZone": timezone}, "end": {"dateTime": input_data.end, "timeZone": timezone}})
    saved = service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
    saved["calendarId"] = calendar_id
    return saved


@app.put("/api/events/{event_id}")
async def update_event(event_id: str, input_data: EventInput, timezone: str = "America/Toronto", calendar_id: str = "primary", user: AuthUser = Depends(calendar_user)):
    """Update one Google Calendar event directly from the timeline editor."""
    await load_user_facts(user)
    try:
        saved = await run_in_threadpool(apply_event_update, event_id, input_data, timezone, calendar_id)
        return serialize_event(saved, timezone)
    except HTTPException: raise
    except Exception as exc: raise HTTPException(status_code=502, detail=f"Calendar update failed: {exc}") from exc

@app.delete("/api/events/{event_id}")
async def delete_event(event_id: str, calendar_id: str = "primary", user: AuthUser = Depends(calendar_user)):
    try:
        await run_in_threadpool(lambda: get_calendar_service().events().delete(calendarId=calendar_id, eventId=event_id).execute())
        return {"message": "Event deleted."}
    except Exception as exc: raise HTTPException(status_code=502, detail=f"Calendar deletion failed: {exc}") from exc


@app.get("/api/conversations")
async def list_conversations(user: AuthUser = Depends(current_user)):
    # The sidebar only needs the most recently updated chats.
    return {"conversations": await db_list_conversations(user)}


@app.post("/api/conversations")
async def create_conversation(input_data: ConversationInput, user: AuthUser = Depends(current_user)):
    return await db_create_conversation(user)


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: AuthUser = Depends(current_user)):
    conversation = await db_get_conversation(user, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.post("/api/schedule/upload")
async def upload_schedule(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(None),
    timezone: str = Form("America/Toronto"),
    name: str = Form("there"),
    user: AuthUser = Depends(current_user),
):
    # Validate the upload before sending it to Gemini for analysis.
    mime_type = file.content_type or ""
    if mime_type not in SUPPORTED_SCHEDULE_TYPES:
        raise HTTPException(status_code=415, detail="Upload a PDF, PNG, JPG, or WEBP schedule.")
    file_bytes = await file.read()
    if not file_bytes or len(file_bytes) > MAX_SCHEDULE_SIZE:
        raise HTTPException(status_code=413, detail="Use a schedule file smaller than 15 MB.")
    try:
        # Gemini's SDK is synchronous; keep the long analysis off the event loop.
        schedule = await run_in_threadpool(extract_schedule, file_bytes, mime_type, timezone)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="I couldn't read a class schedule from that file. Try a clearer image or PDF.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Schedule analysis failed: {exc}") from exc
    if not schedule["classes"]:
        raise HTTPException(status_code=422, detail="I couldn't identify any complete classes. Try a clearer schedule image or PDF.")

    conversation = await get_or_create_conversation(user, conversation_id, "Import class schedule")
    # Store the extracted schedule, but never add events until the user confirms.
    conversation["pending_schedule"] = schedule
    now = datetime.now().isoformat()
    uploaded_message = {
        "role": "user",
        "text": file.filename or "Uploaded schedule",
        "created_at": now,
    }
    # Keep a private inline preview for ordinary schedule screenshots. Large
    # files and PDFs remain filename-only to avoid bloating conversation data.
    if mime_type.startswith("image/") and len(file_bytes) <= MAX_SAVED_PREVIEW_SIZE:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        uploaded_message["image_url"] = f"data:{mime_type};base64,{encoded}"
    conversation["messages"].append(uploaded_message)
    questions = list(schedule["missing"])
    if any(not item.get("location") for item in schedule["classes"]):
        questions.append("Would you like to add locations for any classes?")
    if any(not item.get("start_date") or not item.get("end_date") for item in schedule["classes"]):
        questions.append("What are the first and last dates of the term?")
    questions.append("When you are happy with these details, reply ‘add all classes’ and I’ll create the recurring events.")
    reply = f"I found these classes:\n{schedule_summary(schedule)}\n\nBefore I add anything to Google Calendar, { ' '.join(questions) }"
    conversation["messages"].append({"role": "bot", "text": reply, "created_at": now})
    await db_save_conversation(user, conversation)
    return {"response": reply, "conversation_id": conversation["id"], "title": conversation["title"], "classes": schedule["classes"]}


@app.put("/api/schedule/{conversation_id}")
async def update_schedule_review(conversation_id: str, input_data: ScheduleReviewInput, user: AuthUser = Depends(current_user)):
    """Save the user's edits to an extracted schedule without importing it yet."""
    conversation = await db_get_conversation(user, conversation_id)
    if not conversation or not conversation.get("pending_schedule"):
        raise HTTPException(status_code=404, detail="There is no pending schedule import in this chat.")
    classes = merge_duplicate_classes([item.model_dump() for item in input_data.classes])
    validate_schedule(classes)
    conversation["pending_schedule"]["classes"] = classes
    conversation["pending_schedule"]["missing"] = []
    await db_save_conversation(user, conversation)
    return {"classes": classes, "message": "Schedule details saved. Review them once more, then add your classes."}


@app.delete("/api/schedule/{conversation_id}")
async def cancel_schedule_import(conversation_id: str, user: AuthUser = Depends(current_user)):
    """Discard an extracted schedule before anything reaches Google Calendar."""
    conversation = await db_get_conversation(user, conversation_id)
    if not conversation or not conversation.get("pending_schedule"):
        raise HTTPException(status_code=404, detail="There is no pending schedule import in this chat.")
    conversation.pop("pending_schedule", None)
    await db_save_conversation(user, conversation)
    return {"message": "Schedule import cancelled. No calendar events were added."}


@app.post("/api/schedule/{conversation_id}/import")
async def import_schedule(conversation_id: str, timezone: str = "America/Toronto", user: AuthUser = Depends(calendar_user)):
    """Create weekly Google Calendar series only after the reviewed import is confirmed."""
    conversation = await db_get_conversation(user, conversation_id)
    schedule = conversation.get("pending_schedule") if conversation else None
    if not schedule:
        raise HTTPException(status_code=404, detail="There is no pending schedule import in this chat.")
    classes = schedule["classes"]
    validate_schedule(classes)

    def create_series():
        """Blocking Google Calendar writes, reporting how far they got."""
        created = 0
        for item in classes:
            term_end = datetime.fromisoformat(item["end_date"]).strftime("%Y%m%dT235959Z")
            for weekday in item["days"]:
                start, end = first_class_occurrence(item, weekday, timezone)
                _, rrule_day = WEEKDAYS[weekday.lower()]
                result = create_new_calendar_event(
                    summary=item["name"], start_time=start.isoformat(), end_time=end.isoformat(),
                    time_zone=timezone, location=item.get("location") or None,
                    description="Imported class schedule by Cal.",
                    recurrence=[f"RRULE:FREQ=WEEKLY;BYDAY={rrule_day};UNTIL={term_end}"],
                )
                if isinstance(result, str) and result.startswith('{"error"'):
                    raise RuntimeError(f"stopped after {created} recurring series: {result}")
                created += 1
        return created

    try:
        created = await run_in_threadpool(create_series)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Calendar import {exc}") from exc
    conversation.pop("pending_schedule", None)
    now = datetime.now().isoformat()
    reply = f"Added {created} weekly class {'series' if created == 1 else 'series in total'} to Google Calendar."
    conversation["messages"].append({"role": "bot", "text": reply, "created_at": now})
    await db_save_conversation(user, conversation)
    return {"message": reply, "created": created}


@app.post("/api/chat")
async def chat_with_ai(input_data: ChatInput, user: AuthUser = Depends(calendar_user)):
    # Save the user message first so the assistant can use recent chat memory.
    now = datetime.now().isoformat()
    conversation = await get_or_create_conversation(user, input_data.conversation_id, input_data.message)
    conversation_id = conversation["id"]

    conversation["messages"].append({"role": "user", "text": input_data.message, "created_at": now})
    recent_messages = conversation["messages"][-12:]
    context = "\n".join(f"{item['role'].title()}: {item['text']}" for item in recent_messages)
    # Loading the profile also arms the preference guardrails in the calendar tools.
    profile = await load_user_facts(user)
    now_local = datetime.now(ZoneInfo(input_data.timezone))
    today = now_local.strftime("%A, %Y-%m-%d")
    # Without the clock time, "tonight" or "later" cannot be placed relative to
    # now, and Cal answers questions about times that have already passed.
    current_time = now_local.strftime("%-I:%M %p")
    # Give each model request the current conversation and any pending import.
    instruction = f"""You are Cal, {input_data.name}'s thoughtful personal calendar assistant.
Today is {today} and the current local time is {current_time}; their timezone is {input_data.timezone}.
Words like "tonight", "later", "this afternoon" are relative to that current time. If the user asks about a time that has already passed today, say so rather than answering as if it were still upcoming.
You can access their Google Calendar to create, delete, update events and find availability. Always use safe_create_calendar_event to create an event. If it returns needs_confirmation, explain the concern, offer any alternatives, and ask whether the user wants to keep the proposed time anyway. Only call it again with confirmed=true after the user clearly says yes. This applies to overlaps, tight transitions, and preference warnings.
Use the conversation memory below to make advice and planning suggestions personal. If it lacks useful details, offer a simple, practical suggestion instead of inventing facts.
When the user wants to schedule or find time for something, ask focused follow-up questions if necessary: preferred day or deadline, duration, location/travel needs, and any priority or time-of-day preference. Do not ask questions you can answer from their calendar or remembered facts.
For naturally variable plans such as lunch, coffee, errands, or workouts, ask for duration before checking conflicts or creating an event unless the user explicitly supplied one or Cal has a saved duration preference. Do not assume that mentioning a plan automatically means it should be added to Google Calendar; ask whether they want it scheduled when that is unclear.
Before deleting or significantly modifying an event, confirm the details and wait for approval. Creating events and checking availability do not require confirmation.
When times are supplied, convert them to ISO 8601. Use {input_data.timezone} unless the user explicitly gives a different IANA timezone.

Recent conversation memory:
{context}

Long-term user profile: {json.dumps(profile)}

Pending class-schedule import, if any:
{json.dumps(conversation.get('pending_schedule', {}))}
If there is a pending class-schedule import, ask for missing term dates or locations. Do not create any imported class events until the user explicitly confirms with a clear instruction such as “add all classes.” Once they confirm and all required dates are known, create every class as a weekly recurring Google Calendar event on each listed day through the term."""
    def ask_cal():
        """Blocking Gemini turn, including any calendar tool calls it makes."""
        def once(model):
            chat = client.chats.create(
                model=model,
                config=types.GenerateContentConfig(tools=calendar_tools, temperature=0.5, system_instruction=instruction),
            )
            return chat.send_message(input_data.message)
        return call_gemini(once)

    try:
        # The SDK is synchronous and a turn with tool calls can take tens of
        # seconds, which would otherwise stall every other request.
        response = await run_in_threadpool(ask_cal)
        reply = response.text or "I couldn't create a response. Please try again."
    except GeminiUnavailable as exc:
        # Raw provider JSON is noise to the user; say what happened plainly.
        raise HTTPException(status_code=503, detail="Cal is busy right now — Google's model is overloaded. Try again in a moment.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backend Error: {exc}") from exc

    # Save Cal's reply too, so it is available when the user returns to this chat.
    conversation["messages"].append({"role": "bot", "text": reply, "created_at": datetime.now().isoformat()})
    # Quietly extract durable personal facts after each conversation turn.
    try:
        learned = await run_in_threadpool(lambda: call_gemini(lambda model: client.models.generate_content(model=model, contents=f"Extract durable personal facts/preferences from this message only. Return JSON {{\"facts\":[\"...\"]}}. Do not include temporary plans, sensitive data, or guesses. Message: {input_data.message}", config=types.GenerateContentConfig(temperature=0)), attempts=1))
        facts = clean_json(learned.text or "{}").get("facts", [])
        profile["facts"] = list(dict.fromkeys((profile.get("facts", []) + facts)))[-30:]
        await db_save_profile(user, {"facts": profile["facts"]})
    except Exception: pass
    await db_save_conversation(user, conversation)
    return {"response": reply, "conversation_id": conversation_id, "title": conversation["title"]}
