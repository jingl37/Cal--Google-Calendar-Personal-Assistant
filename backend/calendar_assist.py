import os.path
import json
from contextvars import ContextVar

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError

from dateutil import parser
from zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/calendar"]
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
CURRENT_CREDENTIALS = ContextVar("google_calendar_credentials", default=None)


def set_calendar_credentials(credentials):
    """Bind one signed-in user's credentials to the current request context."""
    CURRENT_CREDENTIALS.set(credentials)


def get_calendar_service():
    """Build a Calendar client for the user bound to this API request."""
    creds = CURRENT_CREDENTIALS.get()
    if not creds:
        raise RuntimeError("Google Calendar is not connected for this user.")
    service = build('calendar', 'v3', credentials=creds)
    return service

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
    if not service:
        return json.dumps({"error": "Failed to get Google Calendar service. Check authentication."})

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

def get_calendar_event_id(event_name: str):
    """Internal helper to return event id by event name"""
    event_id = None

    service = get_calendar_service()
    if not service:
        return json.dumps({"error": "Failed to get Google Calendar service. Check authentication."})

    page_token = None
    while True:
        try:
            events_result = service.events().list(
                calendarId="primary", pageToken=page_token, maxResults=250
            ).execute()
        except HttpError as e:
            return json.dumps({"error": f"Failed to list events: {e.reason}", "status_code": e.resp.status})

        events = events_result.get("items", [])
        for event in events:
            if event.get("summary") == event_name:
                return event["id"]

        page_token = events_result.get('nextPageToken')
        if not page_token:
            break

    return None

def delete_calendar_event(event_name: str):
    """Delete an event from calender using its summary/title name"""
    service = get_calendar_service()
    if not service:
        return json.dumps({"error": "Failed to get Google Calendar service. Check authentication."})

    event_id = get_calendar_event_id(event_name)

    if event_id is None:
        return f'Event "{event_name}" not found.'

    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as e:

        if e.resp.status == 410:
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
    service = get_calendar_service()
    if not service:
        return json.dumps({"error": "Failed to get Google Calendar service. Check authentication."})

    page_token = None
    all_events = []
    while True:
        try:
            events_result = service.events().list(
                calendarId="primary",
                timeMin=start_date_time,
                timeMax=end_date_time,
                pageToken=page_token,
                maxResults=250
            ).execute()
        except HttpError as e:
            return json.dumps({"error": f"Failed to fetch events: {e.reason}", "status_code": e.resp.status})

        events = events_result.get("items", [])
        all_events.extend(events)
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break

    return all_events

def update_calendar_event(event_name: str, changes_to_event: dict):
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

    """
    service = get_calendar_service()
    if not service:
        return json.dumps({"error": "Failed to get Google Calendar service. Check authentication."})

    event_id = get_calendar_event_id(event_name)

    if event_id is None:
        return f'Event "{event_name}" not found.'

    try:
        event = service.events().get(calendarId="primary", eventId=event_id).execute()
        event.update(changes_to_event)
        service.events().update(calendarId="primary", eventId=event_id, body=event).execute()
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
    service = get_calendar_service()
    if not service:
        return json.dumps({"error": "Failed to get Google Calendar service."})

    # build ISO timestamps for the window boundaries
    window_start = parser.isoparse(f"{date}{start_time}").replace(tzinfo=ZoneInfo(time_zone))
    window_end = parser.isoparse(f"{date}{end_time}").replace(tzinfo=ZoneInfo(time_zone))

    try:
        events_result = service.events().list(
            calendarId="primary",
            timeMin=window_start.isoformat(),
            timeMax=window_end.isoformat(),
            singleEvents=True,
            orderBy="startTime"
        ).execute()
    except HttpError as e:
        return json.dumps({"error": f"Failed to fetch events: {e.reason}", "status_code": e.resp.status})

    events = events_result.get("items", [])

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

if __name__ == "__main__":
  get_calendar_service()
#   print(get_calendar_event("2026-07-01T00:00:00-04:00", "2026-07-01T23:00:00-04:00"))
  print(get_calendar_free_slot("2026-07-01", "America/New_York"))
#   delete_calendar_event("Test Event")
#   create_new_calendar_event(
#     summary="Test Event",
#     start_time="2026-04-05T10:00:00",
#     end_time="2026-04-05T11:00:00",
#     time_zone="America/New_York"
# )
