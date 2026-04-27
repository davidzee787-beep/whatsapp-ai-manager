"""
Google Calendar and Sheets service integration.
Handles tasks (with stages) and calendar events.
"""
import os, json, re
from datetime import datetime, timezone, timedelta
from typing import Optional

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
_TOKEN_FILE = os.path.join(_BASE_DIR, "token.json")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",   # only files created by this app
]

# Task status stages (in order)
TASK_STATUSES = ["📋 To Do", "🔄 In Progress", "👀 Review", "✅ Done", "❌ Cancelled"]
TASK_PRIORITIES = ["🔴 High", "🟡 Medium", "🟢 Low"]

# Sheet names
SHEET_TASKS    = os.environ.get("GOOGLE_SHEET_TASKS",    "Tasks")
SHEET_MEETINGS = os.environ.get("GOOGLE_SHEET_MEETINGS", "Meetings")
SHEET_DOCS      = os.environ.get("GOOGLE_SHEET_DOCS",     "Documents")

SHEET_CHATS     = os.environ.get("GOOGLE_SHEET_CHATS", "Chat History")
TASK_HEADERS    = ["#", "Day", "Due Date", "Time", "Task Name", "Priority", "Status", "Category", "Description", "Created At", "Updated At"]
MEETING_HEADERS = ["#", "Title", "Start", "End", "Duration", "Meet Link", "Calendar Link", "Logged At"]
DOC_HEADERS     = ["#", "Filename", "Type", "Drive Link", "Pages", "Notes", "Uploaded By", "Logged At"]
CHAT_HEADERS    = ["#", "Timestamp", "Phone", "Direction", "Message", "Type"]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _get_oauth_credentials():
    """Try to load user OAuth token (preferred — supports Meet links).
    Priority: 1) local token.json file (always freshest), 2) env var fallback."""
    # Option 1: local file FIRST — it gets refreshed and written back, so it's always freshest
    if os.path.exists(_TOKEN_FILE):
        try:
            creds = UserCredentials.from_authorized_user_file(_TOKEN_FILE, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                print("🔄 Refreshing OAuth token from file...")
                creds.refresh(Request())
                with open(_TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
                print("✅ OAuth token refreshed and saved")
            if creds and creds.valid:
                return creds
        except Exception as e:
            print(f"⚠️ OAuth file load/refresh error: {e}")

    # Option 2: env var (for Render where there's no persistent file)
    env_json = os.environ.get("GOOGLE_OAUTH_TOKEN_JSON", "").strip()
    if env_json:
        try:
            info = json.loads(env_json)
            creds = UserCredentials.from_authorized_user_info(info, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                print("🔄 Refreshing OAuth token from env var...")
                creds.refresh(Request())
                print("✅ OAuth env token refreshed")
            return creds if creds and creds.valid else None
        except Exception as e:
            print(f"⚠️ GOOGLE_OAUTH_TOKEN_JSON error: {e}")
            return None

    return None

def _get_credentials():
    # Option 1: Full JSON in env variable (for Render deployment)
    json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_str:
        info = json.loads(json_str)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    # Option 1b: GOOGLE_SERVICE_ACCOUNT_FILE may contain JSON content by mistake
    # If it starts with "{", treat it as JSON content, not a path
    file_value = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if file_value.startswith("{"):
        print("ℹ️ GOOGLE_SERVICE_ACCOUNT_FILE contains JSON content — using as JSON")
        info = json.loads(file_value)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    # Option 2: File path — try absolute, then relative to this script's directory
    file_name = file_value or "service_account.json"
    if os.path.isabs(file_name) and os.path.exists(file_name):
        path = file_name
    else:
        # Try relative to script directory first
        candidate = os.path.join(_BASE_DIR, file_name)
        if os.path.exists(candidate):
            path = candidate
        elif os.path.exists(file_name):
            path = file_name
        else:
            raise FileNotFoundError(
                f"service_account.json not found. Looked in:\n"
                f"  - {candidate}\n"
                f"  - {os.path.abspath(file_name)}\n"
                f"Place the file at: {_BASE_DIR}"
            )
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)

def _calendar_service():
    """Calendar uses OAuth (your account) if available — required for Meet links.
    Falls back to service account if no token.json."""
    oauth = _get_oauth_credentials()
    if oauth:
        return build("calendar", "v3", credentials=oauth)
    return build("calendar", "v3", credentials=_get_credentials())

def _sheets_service():
    # Sheets can use either, prefer OAuth if available
    oauth = _get_oauth_credentials()
    if oauth:
        return build("sheets", "v4", credentials=oauth)
    return build("sheets", "v4", credentials=_get_credentials())

def _spreadsheet_id():
    """Return spreadsheet ID. Auto-extracts the ID if a full URL was pasted."""
    raw = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
    if not raw:
        return ""
    # Extract ID from a full URL like:
    # https://docs.google.com/spreadsheets/d/1ABCdef.../edit#gid=0
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw)
    if m:
        return m.group(1)
    # Already just an ID
    return raw


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------
def _ensure_sheet_exists(service, sheet_name: str):
    """Create the sheet tab if it doesn't exist."""
    sid = _spreadsheet_id()
    meta = service.spreadsheets().get(spreadsheetId=sid).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if sheet_name not in existing:
        print(f"📋 Creating sheet tab: {sheet_name}")
        service.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()

def ensure_all_sheets() -> dict:
    """Proactively create all required sheet tabs with headers — call on startup."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set in .env"}
    try:
        service = _sheets_service()
        # Test access
        meta = service.spreadsheets().get(spreadsheetId=sid).execute()
        title = meta.get("properties", {}).get("title", "?")
        print(f"📊 Spreadsheet accessible: '{title}' (id={sid[:20]}...)")

        for sheet, headers in [
            (SHEET_TASKS,    TASK_HEADERS),
            (SHEET_MEETINGS, MEETING_HEADERS),
            (SHEET_DOCS,     DOC_HEADERS),
            (SHEET_CHATS,    CHAT_HEADERS),
        ]:
            _ensure_sheet_exists(service, sheet)
            _ensure_headers(service, sheet, headers)
            print(f"   ✅ Sheet ready: {sheet}")
        return {"success": True}
    except Exception as e:
        import traceback
        print(f"❌ ensure_all_sheets failed:")
        print(traceback.format_exc())
        return {"success": False, "error": str(e)}

def _ensure_headers(service, sheet_name: str, headers: list[str]):
    """Create header row with formatting if sheet is empty."""
    sid = _spreadsheet_id()
    result = service.spreadsheets().values().get(
        spreadsheetId=sid,
        range=f"{sheet_name}!A1:Z1"
    ).execute()

    if not result.get("values"):
        # Write headers
        service.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"{sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
        ).execute()

        # Format header row: bold, background color, freeze row
        sheet_id = _get_sheet_id(service, sid, sheet_name)
        if sheet_id is not None:
            requests = [
                # Bold + background color for header
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.35},
                                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 11},
                                "horizontalAlignment": "CENTER",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                    }
                },
                # Freeze header row
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]
            service.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": requests}
            ).execute()


def _get_sheet_id(service, spreadsheet_id: str, sheet_name: str) -> Optional[int]:
    """Get the numeric sheet ID for a given sheet name."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet_name:
            return s["properties"]["sheetId"]
    return None


def _get_next_row_num(service, sheet_name: str) -> int:
    """Return the next task/meeting number based on existing rows."""
    sid = _spreadsheet_id()
    result = service.spreadsheets().values().get(
        spreadsheetId=sid,
        range=f"{sheet_name}!A:A"
    ).execute()
    values = result.get("values", [])
    # Subtract 1 for header row
    return max(len(values), 1)


def _color_for_status(status: str) -> dict:
    colors = {
        "📋 To Do":       {"red": 0.9,  "green": 0.9,  "blue": 1.0},
        "🔄 In Progress": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "👀 Review":      {"red": 0.85, "green": 0.95, "blue": 0.85},
        "✅ Done":        {"red": 0.8,  "green": 1.0,  "blue": 0.8},
        "❌ Cancelled":   {"red": 1.0,  "green": 0.85, "blue": 0.85},
    }
    return colors.get(status, {"red": 1, "green": 1, "blue": 1})


def _color_for_priority(priority: str) -> dict:
    colors = {
        "🔴 High":   {"red": 1.0,  "green": 0.85, "blue": 0.85},
        "🟡 Medium": {"red": 1.0,  "green": 1.0,  "blue": 0.8},
        "🟢 Low":    {"red": 0.85, "green": 1.0,  "blue": 0.85},
    }
    return colors.get(priority, {"red": 1, "green": 1, "blue": 1})


# ---------------------------------------------------------------------------
# TASK functions
# ---------------------------------------------------------------------------
def _split_date_time(due_date: str) -> tuple[str, str, str]:
    """Parse due_date string and return (day_name, date_str, time_str)."""
    if not due_date:
        return ("", "", "")
    s = due_date.strip()
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M",
            "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y %H:%M", "%d %B %Y"]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s[:len(s)], fmt)
            day  = dt.strftime("%A")        # Monday, Tuesday...
            date = dt.strftime("%d %b %Y")  # 27 Apr 2026
            time = dt.strftime("%I:%M %p") if "%H" in fmt else ""
            return (day, date, time)
        except ValueError:
            continue
    return ("", s, "")  # fallback: unparseable, keep original

def add_task(
    task_name: str,
    description: str = "",
    category: str = "General",
    priority: str = "🟡 Medium",
    due_date: str = "",
    status: str = "📋 To Do",
) -> dict:
    """Add a new task to the Tasks sheet with formatting."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
        _ensure_sheet_exists(service, SHEET_TASKS)
        _ensure_headers(service, SHEET_TASKS, TASK_HEADERS)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row_num = _get_next_row_num(service, SHEET_TASKS)

        day, date_str, time_str = _split_date_time(due_date)
        # Row order matches new TASK_HEADERS:
        # ["#", "Day", "Due Date", "Time", "Task Name", "Priority", "Status", "Category", "Description", "Created At", "Updated At"]
        row = [row_num, day, date_str, time_str, task_name, priority, status, category, description, now, now]

        result = service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"{SHEET_TASKS}!A:K",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        # Apply row color based on status
        updated_range = result.get("updates", {}).get("updatedRange", "")
        sheet_id = _get_sheet_id(service, sid, SHEET_TASKS)

        if sheet_id is not None and updated_range:
            actual_row = row_num  # 0-indexed row (row_num = data row index)
            bg_color = _color_for_status(status)
            service.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": actual_row,
                            "endRowIndex": actual_row + 1,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": bg_color}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }]},
            ).execute()

        return {"success": True, "task_id": row_num, "task_name": task_name, "status": status}

    except HttpError as e:
        return {"success": False, "error": str(e)}


def get_tasks(status_filter: str = "", max_rows: int = 50) -> dict:
    """Read tasks from the Tasks sheet."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=sid,
            range=f"{SHEET_TASKS}!A1:K{max_rows + 1}",
        ).execute()

        rows = result.get("values", [])
        if not rows:
            return {"success": True, "tasks": []}

        headers = rows[0]
        tasks = [dict(zip(headers, row)) for row in rows[1:] if row]

        if status_filter:
            tasks = [t for t in tasks if status_filter.lower() in t.get("Status", "").lower()]

        return {"success": True, "tasks": tasks}

    except HttpError as e:
        return {"success": False, "error": str(e)}


def update_task_status(task_id: int, new_status: str) -> dict:
    """Update the status of a task by its ID number."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    # Match partial status names
    matched_status = next(
        (s for s in TASK_STATUSES if new_status.lower() in s.lower()), None
    )
    if not matched_status:
        return {"success": False, "error": f"Invalid status. Choose from: {', '.join(TASK_STATUSES)}"}

    try:
        service = _sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=sid,
            range=f"{SHEET_TASKS}!A:K",
        ).execute()

        rows = result.get("values", [])
        row_index = None
        for i, row in enumerate(rows):
            if row and str(row[0]) == str(task_id):
                row_index = i
                break

        if row_index is None:
            return {"success": False, "error": f"Task #{task_id} not found"}

        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # New column layout: A=#, B=Day, C=Date, D=Time, E=Task, F=Priority, G=Status, H=Category, I=Desc, J=Created, K=Updated
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sid,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"{SHEET_TASKS}!G{row_index + 1}", "values": [[matched_status]]},
                    {"range": f"{SHEET_TASKS}!K{row_index + 1}", "values": [[now]]},
                ],
            },
        ).execute()

        # Update row background color
        sheet_id = _get_sheet_id(service, sid, SHEET_TASKS)
        if sheet_id is not None:
            bg_color = _color_for_status(matched_status)
            service.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": bg_color}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }]},
            ).execute()

        return {"success": True, "task_id": task_id, "new_status": matched_status}

    except HttpError as e:
        return {"success": False, "error": str(e)}


def delete_task(task_id: int) -> dict:
    """Mark a task as cancelled."""
    return update_task_status(task_id, "cancelled")


# ---------------------------------------------------------------------------
# CALENDAR functions
# ---------------------------------------------------------------------------
def _parse_dt(dt_str: str) -> str:
    """
    Parse any datetime string Claude might pass and return proper ISO 8601.
    Handles: '2024-04-27T14:00:00', '2024-04-27 14:00', '27 April 2024 2pm',
             'tomorrow 2pm', 'today 3:30pm', relative expressions, etc.
    Returns string like '2024-04-27T14:00:00' (Google will apply the event timezone).
    """
    if not dt_str:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    s = dt_str.strip()

    # Already proper ISO with T — just clean it up
    if re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', s):
        # Strip timezone suffix if present, Google uses event timeZone field
        s = re.sub(r'[+-]\d{2}:\d{2}$', '', s)
        s = re.sub(r'Z$', '', s)
        return s[:19]

    # Date with space instead of T  e.g. "2024-04-27 14:00:00"
    if re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', s):
        return s[:16].replace(' ', 'T') + ':00'

    now = datetime.now()

    # Relative: "tomorrow", "today", "day after tomorrow"
    base_date = None
    s_lower = s.lower()
    if 'day after tomorrow' in s_lower:
        base_date = now + timedelta(days=2)
        s_lower = s_lower.replace('day after tomorrow', '').strip()
    elif 'tomorrow' in s_lower:
        base_date = now + timedelta(days=1)
        s_lower = s_lower.replace('tomorrow', '').strip()
    elif 'today' in s_lower:
        base_date = now
        s_lower = s_lower.replace('today', '').strip()

    # Try to extract time from remaining string
    def extract_time(text):
        # e.g. "2pm", "2:30pm", "14:00", "2 pm", "2:30 PM"
        m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text, re.IGNORECASE)
        if not m:
            return 9, 0  # default 9am
        h, mins, meridiem = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or '').lower()
        if meridiem == 'pm' and h != 12: h += 12
        if meridiem == 'am' and h == 12: h = 0
        return h, mins

    if base_date is not None:
        h, mins = extract_time(s_lower)
        dt = base_date.replace(hour=h, minute=mins, second=0, microsecond=0)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Try common date formats
    formats = [
        "%d %B %Y %I%p", "%d %B %Y %I:%M%p",
        "%d %B %Y %I %p", "%d %B %Y %I:%M %p",
        "%d %B %Y", "%B %d %Y",
        "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M",
        "%d-%m-%Y %H:%M", "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue

    # Last resort — return as-is or now+1hr
    print(f"⚠️ Could not parse datetime: {dt_str!r} — using now+1hr")
    return (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")


def create_calendar_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    attendees: Optional[list] = None,
    location: str = "",
    add_meet: bool = True,
) -> dict:
    import uuid
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

    # Normalize datetimes — Claude may pass in many formats
    start_iso = _parse_dt(start_datetime)
    end_iso   = _parse_dt(end_datetime)
    print(f"📅 Parsed: start={start_iso}  end={end_iso}")

    event = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": "Asia/Karachi"},
        "end":   {"dateTime": end_iso,   "timeZone": "Asia/Karachi"},
    }
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = [{"email": e} for e in attendees]

    # Auto-attach Google Meet link
    if add_meet:
        event["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    # Never send email invites — just create the event and return the Meet link
    event.pop("attendees", None)

    def _try_insert(evt, with_meet=True):
        return _calendar_service().events().insert(
            calendarId=calendar_id,
            body=evt,
            sendUpdates="none",
            conferenceDataVersion=1 if with_meet else 0,
        ).execute()

    created = None
    last_error = None

    # Attempt 1: with Meet link
    try:
        created = _try_insert(event, with_meet=True)
        print(f"✅ Event created with Meet attempt")
    except HttpError as e:
        last_error = str(e)
        print(f"⚠️ First attempt failed: {last_error[:200]}")

        # Attempt 2: without Meet link (some service accounts can't create Meet)
        if "conference" in last_error.lower() or "meet" in last_error.lower() or "hangout" in last_error.lower():
            try:
                event.pop("conferenceData", None)
                created = _try_insert(event, with_meet=False)
                print(f"✅ Event created without Meet (service account limitation)")
            except HttpError as e2:
                last_error = str(e2)
                print(f"❌ Second attempt also failed: {last_error[:200]}")

    if created is None:
        # Real failure — return clear error
        err_msg = last_error or "Unknown error"
        if "notFound" in err_msg or "not found" in err_msg.lower():
            err_msg = f"Calendar not found. Check GOOGLE_CALENDAR_ID in .env, and make sure service account email has been shared with the calendar (Settings → Share with specific people → 'Make changes to events')."
        elif "forbidden" in err_msg.lower() or "permission" in err_msg.lower():
            err_msg = f"Permission denied. Share your Google Calendar with the service account email (give 'Make changes to events' access)."
        elif "invalid" in err_msg.lower() and "datetime" in err_msg.lower():
            err_msg = f"Invalid datetime format. Got start={start_iso}, end={end_iso}"
        return {"success": False, "error": err_msg}

    # Extract Meet link (may be empty if attempt 2 succeeded)
    meet_link = created.get("hangoutLink", "")
    if not meet_link:
        for ep in (created.get("conferenceData", {}).get("entryPoints") or []):
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri", "")
                break

    return {
        "success":    True,
        "event_id":   created["id"],
        "event_link": created.get("htmlLink", ""),
        "meet_link":  meet_link,
        "title":      created["summary"],
        "start":      created["start"]["dateTime"],
        "end":        created["end"]["dateTime"],
    }


def list_calendar_events(max_results: int = 10, time_min: Optional[str] = None) -> dict:
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    if time_min is None:
        time_min = datetime.now(timezone.utc).isoformat()

    try:
        result = _calendar_service().events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        return {
            "success": True,
            "events": [
                {
                    "id":          e["id"],
                    "title":       e.get("summary", "(No title)"),
                    "start":       e["start"].get("dateTime", e["start"].get("date")),
                    "end":         e["end"].get("dateTime",   e["end"].get("date")),
                    "description": e.get("description", ""),
                    "link":        e.get("htmlLink", ""),
                    "meet_link":   e.get("hangoutLink", ""),
                    "location":    e.get("location", ""),
                }
                for e in result.get("items", [])
            ],
        }
    except HttpError as e:
        return {"success": False, "error": str(e)}


def append_meeting_to_sheet(
    title: str,
    start_datetime: str,
    end_datetime: str,
    attendees: str = "",  # kept for backward compat — unused now
    description: str = "",
    event_link: str = "",
    meet_link: str = "",
) -> dict:
    """Log a booked meeting to the Meetings sheet with full details + Meet link."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
        _ensure_sheet_exists(service, SHEET_MEETINGS)
        _ensure_headers(service, SHEET_MEETINGS, MEETING_HEADERS)

        # Calculate duration in minutes
        duration_str = ""
        try:
            s = datetime.fromisoformat(start_datetime[:19])
            e = datetime.fromisoformat(end_datetime[:19])
            mins = int((e - s).total_seconds() / 60)
            duration_str = f"{mins} min" if mins < 60 else f"{mins // 60} hr" + (f" {mins % 60} min" if mins % 60 else "")
        except Exception:
            pass

        row_num = _get_next_row_num(service, SHEET_MEETINGS)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [row_num, title, start_datetime, end_datetime, duration_str, meet_link, event_link, now]

        service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"{SHEET_MEETINGS}!A:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        return {"success": True, "meeting_num": row_num}

    except HttpError as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# DRIVE + DOCUMENTS
# ---------------------------------------------------------------------------
def _drive_service():
    """Drive uses OAuth (your account) — service accounts can't share to your Drive."""
    oauth = _get_oauth_credentials()
    if not oauth:
        return None
    return build("drive", "v3", credentials=oauth)

def upload_to_drive(file_bytes: bytes, filename: str, mime_type: str = "application/pdf",
                    folder_name: str = "WhatsApp Documents") -> dict:
    """Upload a file to Google Drive and return shareable link.
    Files go into a folder named 'WhatsApp Documents' (created if missing).
    """
    import io as _io
    from googleapiclient.http import MediaIoBaseUpload

    drive = _drive_service()
    if drive is None:
        return {"success": False, "error": "Drive requires OAuth — run get_token.py to re-authorize with Drive scope."}

    try:
        # Find or create the folder
        folder_id = None
        q = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        res = drive.files().list(q=q, spaces="drive", fields="files(id,name)").execute()
        items = res.get("files", [])
        if items:
            folder_id = items[0]["id"]
        else:
            folder_meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
            folder = drive.files().create(body=folder_meta, fields="id").execute()
            folder_id = folder.get("id")

        # Upload the file
        media    = MediaIoBaseUpload(_io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
        metadata = {"name": filename, "parents": [folder_id]}
        uploaded = drive.files().create(
            body=metadata, media_body=media,
            fields="id,name,webViewLink,webContentLink"
        ).execute()

        file_id = uploaded["id"]

        # Make link sharable (anyone with link can view)
        try:
            drive.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                fields="id"
            ).execute()
        except HttpError as pe:
            print(f"⚠️ Could not make file public (still uploaded): {pe}")

        view_link = uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        return {"success": True, "file_id": file_id, "link": view_link, "name": filename}

    except HttpError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def append_document_to_sheet(filename: str, drive_link: str, doc_type: str = "PDF",
                              pages: int = 0, notes: str = "", uploaded_by: str = "") -> dict:
    """Log a Drive-uploaded document to the Documents sheet."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
        _ensure_sheet_exists(service, SHEET_DOCS)
        _ensure_headers(service, SHEET_DOCS, DOC_HEADERS)

        row_num = _get_next_row_num(service, SHEET_DOCS)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [row_num, filename, doc_type, drive_link, pages or "", notes, uploaded_by, now]

        service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"{SHEET_DOCS}!A:H",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return {"success": True, "row": row_num}
    except HttpError as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# CHAT HISTORY functions
# ---------------------------------------------------------------------------
def append_chat_history(phone: str, direction: str, message: str, msg_type: str = "text") -> dict:
    """Log a chat message (IN or OUT) to the Chat History sheet."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
        _ensure_sheet_exists(service, SHEET_CHATS)
        _ensure_headers(service, SHEET_CHATS, CHAT_HEADERS)
        row_num = _get_next_row_num(service, SHEET_CHATS)
        now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row     = [row_num, now, phone, direction.upper(), message[:500], msg_type]

        service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"{SHEET_CHATS}!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        # Color: IN = light blue, OUT = light green
        sheet_id = _get_sheet_id(service, sid, SHEET_CHATS)
        if sheet_id is not None:
            bg = {"red": 0.85, "green": 0.93, "blue": 1.0} if direction.upper() == "IN" else {"red": 0.85, "green": 1.0, "blue": 0.88}
            service.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": row_num, "endRowIndex": row_num + 1},
                    "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                    "fields": "userEnteredFormat.backgroundColor",
                }}]},
            ).execute()

        return {"success": True}
    except HttpError as e:
        return {"success": False, "error": str(e)}


def get_chat_history(phone_filter: str = "", max_rows: int = 200) -> dict:
    """Read chat history from the Chat History sheet."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        result = _sheets_service().spreadsheets().values().get(
            spreadsheetId=sid,
            range=f"{SHEET_CHATS}!A1:F{max_rows + 1}",
        ).execute()

        rows = result.get("values", [])
        if not rows:
            return {"success": True, "chats": []}

        headers = rows[0]
        chats   = [dict(zip(headers, r)) for r in rows[1:] if r]

        if phone_filter:
            chats = [c for c in chats if phone_filter in c.get("Phone", "")]

        return {"success": True, "chats": list(reversed(chats))}  # newest first

    except HttpError as e:
        return {"success": False, "error": str(e)}


def get_sheet_meetings(max_rows: int = 20) -> dict:
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        result = _sheets_service().spreadsheets().values().get(
            spreadsheetId=sid,
            range=f"{SHEET_MEETINGS}!A1:H{max_rows + 1}",
        ).execute()

        rows = result.get("values", [])
        if not rows:
            return {"success": True, "meetings": []}

        headers = rows[0]
        return {"success": True, "meetings": [dict(zip(headers, r)) for r in rows[1:] if r]}

    except HttpError as e:
        return {"success": False, "error": str(e)}
