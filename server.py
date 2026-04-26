"""
Dashboard API server — wraps google_services and serves the dashboard UI.
Run: python server.py  then open http://localhost:5000
"""
import os
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from dotenv import load_dotenv

import google_services as gs

load_dotenv()

app = Flask(__name__, static_folder=".")
CORS(app)


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")


@app.route("/dashboard.css")
def dashboard_css():
    return send_from_directory(".", "dashboard.css")


# ---------------------------------------------------------------------------
# API — Calendar
# ---------------------------------------------------------------------------

@app.route("/api/events")
def get_events():
    max_results = int(request.args.get("max", 20))
    result = gs.list_calendar_events(max_results=max_results)
    return jsonify(result)


@app.route("/api/events/today")
def get_today_events():
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    result = gs.list_calendar_events(max_results=50, time_min=today_start.isoformat())
    if result.get("success"):
        result["events"] = [
            e for e in result["events"]
            if e["start"] < today_end.isoformat()
        ]
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Sheets
# ---------------------------------------------------------------------------

@app.route("/api/meetings")
def get_meetings():
    max_rows = int(request.args.get("max", 50))
    result = gs.get_sheet_meetings(max_rows=max_rows)
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Stats rollup
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def get_stats():
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    upcoming = gs.list_calendar_events(max_results=50, time_min=now.isoformat())
    meetings = gs.get_sheet_meetings(max_rows=200)

    today_count = 0
    week_count = 0
    next_event = None

    if upcoming.get("success"):
        events = upcoming["events"]
        today_str = now.date().isoformat()
        for e in events:
            start_date = e["start"][:10]
            if start_date == today_str:
                today_count += 1
            if start_date >= week_start.date().isoformat():
                week_count += 1
        if events:
            next_event = events[0]

    total_logged = (
        len(meetings.get("meetings", [])) if meetings.get("success") else 0
    )

    return jsonify(
        {
            "today_events": today_count,
            "week_events": week_count,
            "total_logged": total_logged,
            "next_event": next_event,
            "current_time": now.isoformat(),
        }
    )


if __name__ == "__main__":
    print("Dashboard running at http://localhost:8080")
    app.run(debug=True, port=8080)
