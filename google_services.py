"""
Google Calendar and Sheets service integration.
Handles tasks (with stages) and calendar events.
"""
import os, json
from datetime import datetime, timezone
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Task status stages (in order)
TASK_STATUSES = ["📋 To Do", "🔄 In Progress", "👀 Review", "✅ Done", "❌ Cancelled"]
TASK_PRIORITIES = ["🔴 High", "🟡 Medium", "🟢 Low"]

# Sheet names
SHEET_TASKS    = os.environ.get("GOOGLE_SHEET_TASKS",    "Tasks")
SHEET_MEETINGS = os.environ.get("GOOGLE_SHEET_MEETINGS", "Meetings")

SHEET_CHATS     = os.environ.get("GOOGLE_SHEET_CHATS", "Chat History")
TASK_HEADERS    = ["#", "Task Name", "Description", "Category", "Priority", "Status", "Due Date", "Created At", "Updated At"]
MEETING_HEADERS = ["#", "Title", "Start", "End", "Attendees", "Description", "Calendar Link", "Logged At"]
CHAT_HEADERS    = ["#", "Timestamp", "Phone", "Direction", "Message", "Type"]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _get_credentials():
    # Option 1: Full JSON in env variable (for Render deployment)
    json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_str:
        info = json.loads(json_str)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    # Option 2: File path — try absolute, then relative to this script's directory
    file_name = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
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
    return build("calendar", "v3", credentials=_get_credentials())

def _sheets_service():
    return build("sheets", "v4", credentials=_get_credentials())

def _spreadsheet_id():
    return os.environ.get("GOOGLE_SPREADSHEET_ID", "")


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------
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
        _ensure_headers(service, SHEET_TASKS, TASK_HEADERS)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row_num = _get_next_row_num(service, SHEET_TASKS)

        row = [row_num, task_name, description, category, priority, status, due_date, now, now]

        result = service.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"{SHEET_TASKS}!A:I",
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
            range=f"{SHEET_TASKS}!A1:I{max_rows + 1}",
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
            range=f"{SHEET_TASKS}!A:I",
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

        # Update status (col F = index 5) and Updated At (col I = index 8)
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sid,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"{SHEET_TASKS}!F{row_index + 1}", "values": [[matched_status]]},
                    {"range": f"{SHEET_TASKS}!I{row_index + 1}", "values": [[now]]},
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
def create_calendar_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    attendees: Optional[list] = None,
    location: str = "",
) -> dict:
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    event = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": "UTC"},
        "end":   {"dateTime": end_datetime,   "timeZone": "UTC"},
    }
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = [{"email": e} for e in attendees]

    try:
        created = _calendar_service().events().insert(
            calendarId=calendar_id, body=event, sendUpdates="all"
        ).execute()
        return {
            "success":    True,
            "event_id":   created["id"],
            "event_link": created.get("htmlLink", ""),
            "title":      created["summary"],
            "start":      created["start"]["dateTime"],
            "end":        created["end"]["dateTime"],
        }
    except HttpError as e:
        return {"success": False, "error": str(e)}


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
    attendees: str = "",
    description: str = "",
    event_link: str = "",
) -> dict:
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
        _ensure_headers(service, SHEET_MEETINGS, MEETING_HEADERS)

        row_num = _get_next_row_num(service, SHEET_MEETINGS)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [row_num, title, start_datetime, end_datetime, attendees, description, event_link, now]

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
# CHAT HISTORY functions
# ---------------------------------------------------------------------------
def append_chat_history(phone: str, direction: str, message: str, msg_type: str = "text") -> dict:
    """Log a chat message (IN or OUT) to the Chat History sheet."""
    sid = _spreadsheet_id()
    if not sid:
        return {"success": False, "error": "GOOGLE_SPREADSHEET_ID not set"}

    try:
        service = _sheets_service()
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
