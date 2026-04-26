"""
Calendar & Sheets Chatbot powered by Claude with tool use.

Setup:
  1. Copy .env.example to .env and fill in your keys.
  2. Share your Google Calendar and Spreadsheet with the service account email.
  3. pip install -r requirements.txt
  4. python chatbot.py
"""
import json
import os
import sys
from typing import Any

import anthropic
from dotenv import load_dotenv

import google_services as gs

load_dotenv()

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "create_calendar_event",
        "description": (
            "Create a meeting or call on Google Calendar. "
            "Automatically logs it to the Google Sheet as well. "
            "Use ISO 8601 format for datetimes, e.g. '2025-05-01T10:00:00Z'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Meeting or call title"},
                "start_datetime": {
                    "type": "string",
                    "description": "Start time in ISO 8601 format, e.g. '2025-05-01T10:00:00Z'",
                },
                "end_datetime": {
                    "type": "string",
                    "description": "End time in ISO 8601 format",
                },
                "description": {
                    "type": "string",
                    "description": "Optional agenda or notes",
                    "default": "",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of attendee email addresses",
                    "default": [],
                },
                "location": {
                    "type": "string",
                    "description": "Meeting room, video link, or address",
                    "default": "",
                },
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "List upcoming events from Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events to return (default 10)",
                    "default": 10,
                },
                "time_min": {
                    "type": "string",
                    "description": "Only return events after this ISO 8601 datetime. Defaults to now.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_sheet_meeting",
        "description": (
            "Manually log a meeting or call to the Google Sheet "
            "(use when you need to record a meeting that was NOT just created via create_calendar_event)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Meeting title"},
                "start_datetime": {"type": "string", "description": "Start time"},
                "end_datetime": {"type": "string", "description": "End time"},
                "attendees": {
                    "type": "string",
                    "description": "Comma-separated list of attendees",
                    "default": "",
                },
                "description": {
                    "type": "string",
                    "description": "Notes or agenda",
                    "default": "",
                },
                "event_link": {
                    "type": "string",
                    "description": "Calendar event or video call link",
                    "default": "",
                },
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "get_sheet_meetings",
        "description": "Read the list of meetings logged in the Google Sheet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 20)",
                    "default": 20,
                }
            },
            "required": [],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Dispatch a tool call and return a JSON string result."""
    if name == "create_calendar_event":
        result = gs.create_calendar_event(
            title=tool_input["title"],
            start_datetime=tool_input["start_datetime"],
            end_datetime=tool_input["end_datetime"],
            description=tool_input.get("description", ""),
            attendees=tool_input.get("attendees") or [],
            location=tool_input.get("location", ""),
        )
        # Auto-log to sheet if calendar event was created successfully
        if result.get("success"):
            gs.append_meeting_to_sheet(
                title=result["title"],
                start_datetime=result["start"],
                end_datetime=result["end"],
                attendees=", ".join(tool_input.get("attendees") or []),
                description=tool_input.get("description", ""),
                event_link=result.get("event_link", ""),
            )
        return json.dumps(result)

    elif name == "list_calendar_events":
        result = gs.list_calendar_events(
            max_results=tool_input.get("max_results", 10),
            time_min=tool_input.get("time_min"),
        )
        return json.dumps(result)

    elif name == "update_sheet_meeting":
        result = gs.append_meeting_to_sheet(
            title=tool_input["title"],
            start_datetime=tool_input["start_datetime"],
            end_datetime=tool_input["end_datetime"],
            attendees=tool_input.get("attendees", ""),
            description=tool_input.get("description", ""),
            event_link=tool_input.get("event_link", ""),
        )
        return json.dumps(result)

    elif name == "get_sheet_meetings":
        result = gs.get_sheet_meetings(max_rows=tool_input.get("max_rows", 20))
        return json.dumps(result)

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Chatbot
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a helpful scheduling assistant. You can:
- Schedule meetings and calls on Google Calendar
- Log meeting details to a Google Sheet
- Show upcoming calendar events
- Show the meetings log from the Google Sheet

When scheduling a meeting, always confirm the details (title, date/time, attendees) with the user
before creating it. If the user doesn't specify a timezone, assume UTC.
When you create a calendar event, it is automatically logged to the Google Sheet as well.

Today's date/time for reference: use ISO 8601 format when calling tools.
Be concise, friendly, and confirm successful actions clearly."""


class CalendarChatbot:
    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("Error: ANTHROPIC_API_KEY not set. Copy .env.example to .env and fill it in.")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.messages: list[dict] = []

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})

        # Agentic loop — keep going until Claude stops calling tools
        while True:
            response = self.client.messages.create(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.messages,
                thinking={"type": "adaptive"},
            )

            # Collect text output for display
            assistant_text = ""
            tool_calls = []

            for block in response.content:
                if block.type == "text":
                    assistant_text += block.text
                elif block.type == "tool_use":
                    tool_calls.append(block)

            # Append assistant turn (full content preserves tool_use blocks)
            self.messages.append({"role": "assistant", "content": response.content})

            # If no tool calls, we're done
            if response.stop_reason == "end_turn" or not tool_calls:
                return assistant_text

            # Execute tools and collect results
            print(f"\n[Using {len(tool_calls)} tool(s)...]")
            tool_results = []
            for tool in tool_calls:
                print(f"  → {tool.name}({json.dumps(tool.input, indent=None)})")
                result = execute_tool(tool.name, tool.input)
                print(f"  ← {result[:120]}{'...' if len(result) > 120 else ''}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": result,
                })

            self.messages.append({"role": "user", "content": tool_results})

    def run(self):
        print("=" * 60)
        print("  Calendar & Sheets Chatbot  (type 'quit' to exit)")
        print("=" * 60)
        print("I can schedule meetings, list your calendar, and log")
        print("meeting details to your Google Sheet.\n")

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_input:
                continue
            if user_input.lower() in {"quit", "exit", "bye"}:
                print("Goodbye!")
                break

            try:
                reply = self.chat(user_input)
                print(f"\nAssistant: {reply}\n")
            except anthropic.APIError as e:
                print(f"\n[API error: {e}]\n")


if __name__ == "__main__":
    CalendarChatbot().run()
