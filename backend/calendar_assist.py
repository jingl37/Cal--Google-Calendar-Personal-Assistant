import json
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone as utc_timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from dateutil import parser
from zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CURRENT_CREDENTIALS = ContextVar("google_calendar_credentials", default=None)
# Listing calendars on every lookup is wasteful, so cache it per request.
CALENDAR_IDS = ContextVar("readable_calendar_ids", default=None)
# Resolving a title against the whole calendar matches events from years ago,
# so title lookups only consider a window around today.
NAME_LOOKUP_PAST = timedelta(days=30)
NAME_LOOKUP_FUTURE = timedelta(days=365)
# Read-only Google subscriptions. They are noise on the timeline and would
# raise phantom "you are double booked" warnings against every holiday.
IGNORED_CALENDAR_SUFFIXES = (
    "#holiday@group.v.calendar.google.com",
    "#contacts@group.v.calendar.google.com",
    "#weeknum@group.v.calendar.google.com",
)


def set_calendar_credentials(credentials):
    """Bind one signed-in user's credentials to the current request context."""
    CURRENT_CREDENTIALS.set(credentials)
    CALENDAR_IDS.set(None)


def get_calendar_service():
    """Build a Calendar client for the user bound to this API request."""
    creds = CURRENT_CREDENTIALS.get()
    if not creds:
        raise RuntimeError("Google Calendar is not connected for this user.")
    service = build('calendar', 'v3', credentials=creds)
    return service


def readable_calendar_ids():
    """Every calendar Cal should read from.

    Users keep most of their life on secondary calendars, so reading only
    "primary" shows an almost empty day. Holiday and birthday feeds are left
    out because they are noise on a timeline and would clash with everything.

    The sidebar "selected" checkbox is deliberately ignored: un-ticking a
    calendar in Google's UI is about decluttering that view, and letting it
    silently hide a whole schedule from Cal is far more confusing than
    showing one extra calendar. Calendars the user explicitly hid are still
    excluded, via showHidden=False.
    """
    cached = CALENDAR_IDS.get()
    if cached is not None:
        return cached
    service = get_calendar_service()
    ids, page_token = [], None
    while True:
        result = service.calendarList().list(pageToken=page_token, showHidden=False).execute()
        for calendar in result.get("items", []):
            calendar_id = calendar.get("id", "")
            if calendar.get("deleted") or any(calendar_id.endswith(suffix) for suffix in IGNORED_CALENDAR_SUFFIXES):
                continue
            ids.append(calendar_id)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    ids = ids or ["primary"]
    CALENDAR_IDS.set(ids)
    return ids


def list_events_in_window(start_iso: str, end_iso: str, single_events: bool = True):
    """Collect events across every readable calendar, tagged with their source."""
    service = get_calendar_service()
    collected = []
    for calendar_id in readable_calendar_ids():
        page_token = None
        while True:
            request = service.events().list(
                calendarId=calendar_id,
                timeMin=start_iso,
                timeMax=end_iso,
                singleEvents=single_events,
                pageToken=page_token,
                maxResults=250,
                **({"orderBy": "startTime"} if single_events else {}),
            )
            result = request.execute()
            for item in result.get("items", []):
                # Remember the owning calendar so edits and deletes can find it.
                item["calendarId"] = calendar_id
                collected.append(item)
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    return sorted(collected, key=event_start)

def create_new_calendar_event(
        summary: str,
        start_time: str,
        end_time: str,
        time_zone: str,
        description: str = None,
        location: str = None,
        recurrence: list[str] = None,
):
    """
    Creates a new event on the user's calender

    Args:
        summary: The title of the event (e.g., 'Lunch with Sarah').
        start_time: Start time in ISO 8601 format (e.g., '2026-06-09T13:00:00').
        end_time: End time in ISO 8601 format (e.g., '2026-06-09T14:00:00').
        time_zone: The IANA timezone string (e.g., 'America/New_York' or 'UTC').
        description: Optional details about the event.
        location: Optional location details.
        recurrence: A list of RFC 5545 recurrence rule strings (e.g., ['FREQ=DAILY'])
    """
    service = get_calendar_service()

    event = {
  "summary": summary,
  "start": {
    "dateTime": start_time,
    "timeZone": time_zone,
  },
  "end": {
    "dateTime": end_time,
    "timeZone": time_zone,
  },
}
    #for optional input
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    if recurrence:
        event["recurrence"] = recurrence

    try:
        service.events().insert(calendarId='primary', body=event).execute()
    except HttpError as e:
        return json.dumps({"error": f"Failed to create event: {e.reason}", "status_code": e.resp.status})

    return f"Event \"{summary}\" created!"

def event_start(event):
    """Sortable start moment for an event, tolerating all-day and malformed entries."""
    value = event.get("start", {})
    text = value.get("dateTime") or value.get("date")
    if not text:
        return datetime.max.replace(tzinfo=utc_timezone.utc)
    moment = parser.isoparse(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=utc_timezone.utc)


def describe_event(event):
    """Short human label used when several events share a title."""
    start = event.get("start", {})
    return f"{event.get('summary', 'Untitled')} on {(start.get('dateTime') or start.get('date') or 'an unknown date')[:10]}"


def find_events_by_name(event_name: str, on_date: str = None):
    """Internal helper returning every event near today whose title matches.

    Recurring events are left unexpanded so that acting on a class series
    affects the whole series rather than a single arbitrary instance.
    """
    now = datetime.now(utc_timezone.utc)
    wanted = event_name.strip().lower()
    candidates = list_events_in_window(
        (now - NAME_LOOKUP_PAST).isoformat(),
        (now + NAME_LOOKUP_FUTURE).isoformat(),
        single_events=False,
    )
    matches = [item for item in candidates if (item.get("summary") or "").strip().lower() == wanted]
    if on_date:
        matches = [item for item in matches if str(event_start(item).date()) == on_date]
    return sorted(matches, key=event_start)


def resolve_event(event_name: str, on_date: str = None):
    """Pick the one event a title refers to, or explain why it is ambiguous."""
    matches = find_events_by_name(event_name, on_date)
    if not matches:
        return None, f'Event "{event_name}" not found.'
    if len(matches) > 1:
        options = "; ".join(describe_event(item) for item in matches[:5])
        return None, f'Several events are called "{event_name}" ({options}). Ask the user which date they mean and pass it as on_date.'
    return matches[0], None

def delete_calendar_event(event_name: str, on_date: str = None):
    """
    Delete an event from the calendar using its summary/title name.

    Args:
        event_name: The exact title of the event to delete (e.g., 'Math Class').
        on_date: Optional 'YYYY-MM-DD' date used to choose between events sharing a title.
    """
    service = get_calendar_service()

    try:
        event, problem = resolve_event(event_name, on_date)
        if problem:
            return problem
        service.events().delete(calendarId=event.get("calendarId", "primary"), eventId=event["id"]).execute()
    except HttpError as e:
        # A 410 means the event was already removed, which is the desired end state.
        if e.resp.status in (404, 410):
            return f"Event \"{event_name}\" deleted!"
        return json.dumps({"error": f"Failed to delete event: {e.reason}", "status_code": e.resp.status})

    return f"Event \"{event_name}\" deleted!"

def get_calendar_event(start_date_time: str, end_date_time: str):
    """
    Returns all events for a chosen time frame.

    Args:
        start_date_time: Start window in ISO 8601 format (e.g., '2026-06-09T00:00:00Z').
        end_date_time: End window in ISO 8601 format (e.g., '2026-06-09T23:59:59Z').
    """
    try:
        # Recurring series are expanded so each real meeting is counted.
        return list_events_in_window(start_date_time, end_date_time)
    except HttpError as e:
        return json.dumps({"error": f"Failed to fetch events: {e.reason}", "status_code": e.resp.status})

def update_calendar_event(event_name: str, changes_to_event: dict, on_date: str = None):
    """
    Updates event by event name

    Args:
        event_name: The current summary/title of the event to modify (e.g., 'Math Class').
        changes_to_event: A dictionary containing the updated fields for the event body.
            Supported keys include:
            - 'summary': (str) New title of the event.
            - 'location': (str) New location.
            - 'description': (str) New description.
            - 'start': (dict) New start time, e.g., {'dateTime': '2026-06-09T14:00:00', 'timeZone': 'UTC'}
            - 'end': (dict) New end time, e.g., {'dateTime': '2026-06-09T15:00:00', 'timeZone': 'UTC'}
        on_date: Optional 'YYYY-MM-DD' date used to choose between events sharing a title.

    """
    service = get_calendar_service()

    try:
        event, problem = resolve_event(event_name, on_date)
        if problem:
            return problem
        calendar_id = event.pop("calendarId", "primary")
        event.update(changes_to_event)
        service.events().update(calendarId=calendar_id, eventId=event["id"], body=event).execute()
    except HttpError as e:
        return json.dumps({"error": f"Failed to update event: {e.reason}", "status_code": e.resp.status})

    return f"Event \"{event_name}\" updated!"

def is_calendar_slot_free(start_date_time: str, end_date_time: str):
    """
    Determines if a time slot is free given start and end time

     Args:
        start_date_time: Start window in ISO 8601 format (e.g., '2026-06-09T00:00:00Z').
        end_date_time: End window in ISO 8601 format (e.g., '2026-06-09T23:59:59Z').

    """
    events = get_calendar_event(start_date_time, end_date_time)

    if isinstance(events, str):
        return events

    return not events

def get_calendar_free_slot(date: str, time_zone: str, start_time: str = "T00:00:00", end_time: str = "T23:59:59"):
    """Returns available free time slots for a given date.

    Args:
        date: The date to check in YYYY-MM-DD format (e.g., '2026-06-09').
        time_zone: The IANA timezone string (e.g., 'America/New_York' or 'UTC').
        start_time: Optional limit start (e.g., 'T09:00:00').
        end_time: Optional limit end (e.g., 'T17:00:00').
    """
    # build ISO timestamps for the window boundaries
    window_start = parser.isoparse(f"{date}{start_time}").replace(tzinfo=ZoneInfo(time_zone))
    window_end = parser.isoparse(f"{date}{end_time}").replace(tzinfo=ZoneInfo(time_zone))

    try:
        events = list_events_in_window(window_start.isoformat(), window_end.isoformat())
    except HttpError as e:
        return json.dumps({"error": f"Failed to fetch events: {e.reason}", "status_code": e.resp.status})

    taken_slots = []

    for event in events:
        start = event.get("start", {}).get("dateTime")
        end = event.get("end", {}).get("dateTime")

        # Skip all-day events (they only have "date", not "dateTime") or malformed events
        if not start or not end:
            continue

        start_dt = parser.isoparse(start)
        end_dt = parser.isoparse(end)

        taken_slots.append((start_dt, end_dt))

    taken_slots.sort(key=lambda x: x[0])

    free_slots = []
    current = window_start

    for slot_start, slot_end in taken_slots:
        if slot_start > current:
            free_slots.append(f"{current.isoformat()} → {slot_start.isoformat()}")
        if slot_end > current:
            current = slot_end

    if current < window_end:
        free_slots.append(f"{current.isoformat()} → {window_end.isoformat()}")

    return free_slots
