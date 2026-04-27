"""
Personal WhatsApp AI Manager — powered by Claude + Supabase
Supports: Text, Images, Documents, Voice Notes, Any Language
Memory: Supabase (persistent conversation history)
"""
import os, re, json, io, base64, threading, requests, anthropic
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime
from typing import Any, Optional
from collections import OrderedDict

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WHATSAPP_TOKEN  = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN", "testwhat")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_KEY      = os.environ.get("OPENAI_API_KEY", "")
GRAPH_URL       = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

# ---------------------------------------------------------------------------
# Owner access control
# ---------------------------------------------------------------------------
# Only this number gets full access (tasks, calendar, everything)
# All other numbers = clients → can only book meetings & chat
OWNER_PHONES = {"923191413828", "3191413828", "03191413828"}  # all formats

def is_owner(phone: str) -> bool:
    """Check if the sender is the owner (Muhammad Daud Zia)."""
    clean = phone.strip().lstrip("+")
    return clean in OWNER_PHONES or clean.endswith("3191413828")

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
try:
    import supabase_services as sb
    SUPABASE_OK = True
    print("✅ Supabase enabled")
except Exception as e:
    sb = None
    SUPABASE_OK = False
    print(f"⚠️  Supabase disabled: {e}")

# ---------------------------------------------------------------------------
# Google services (optional — for Calendar)
# ---------------------------------------------------------------------------
try:
    import google_services as gs
    GOOGLE_OK = True
    print("✅ Google services enabled")
    # Proactively create all sheets — surfaces any access errors right at startup
    try:
        sheet_check = gs.ensure_all_sheets()
        if not sheet_check.get("success"):
            print(f"⚠️ Sheets setup warning: {sheet_check.get('error')}")
    except Exception as _se:
        print(f"⚠️ Sheets setup error: {_se}")
except Exception as e:
    gs = None
    GOOGLE_OK = False
    print(f"⚠️  Google disabled: {e}")

# ---------------------------------------------------------------------------
# OpenAI Whisper
# ---------------------------------------------------------------------------
try:
    import openai as oai_lib
    oai_client = oai_lib.OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None
    WHISPER_OK = bool(OPENAI_KEY)
except ImportError:
    oai_client = None
    WHISPER_OK = False

# ---------------------------------------------------------------------------
# PDF reader
# ---------------------------------------------------------------------------
try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False

# ---------------------------------------------------------------------------
# Claude client
# ---------------------------------------------------------------------------
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ---------------------------------------------------------------------------
# Conversation memory — Supabase backed, in-memory cache
# ---------------------------------------------------------------------------
_mem_cache: dict[str, list] = {}

def load_history(phone: str) -> list:
    if phone in _mem_cache:
        return _mem_cache[phone]
    if SUPABASE_OK:
        history = sb.load_conversation(phone)
        _mem_cache[phone] = history
        return history
    return []

def _deep_serialize_history(history: list) -> list:
    """Walk entire history and convert any Anthropic SDK objects to plain dicts.
    Belt-and-suspenders safety net so save_conversation never gets SDK objects."""
    cleaned = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", [])
        if isinstance(content, str):
            cleaned.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict):
                new_content.append(block)
            elif hasattr(block, "type"):
                t = block.type
                if t == "text":
                    new_content.append({"type": "text", "text": getattr(block, "text", "")})
                elif t == "tool_use":
                    new_content.append({
                        "type": "tool_use",
                        "id":   getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input":getattr(block, "input", {}),
                    })
                elif t == "tool_result":
                    new_content.append({
                        "type": "tool_result",
                        "tool_use_id": getattr(block, "tool_use_id", ""),
                        "content":     getattr(block, "content", ""),
                    })
                else:
                    new_content.append({"type": t})
            else:
                new_content.append({"type": "text", "text": str(block)})
        cleaned.append({"role": role, "content": new_content})
    return cleaned

def save_history(phone: str, history: list):
    # Defensive — make sure no Anthropic SDK objects sneak through
    history = _deep_serialize_history(history)
    _mem_cache[phone] = history
    if SUPABASE_OK:
        try:
            sb.save_conversation(phone, history)
        except Exception as e:
            print(f"⚠️ Save conversation error: {e} — resetting corrupted history")
            # Last resort: clear and save empty so next message starts fresh
            _mem_cache[phone] = []
            try: sb.save_conversation(phone, [])
            except: pass

# ---------------------------------------------------------------------------
# Document buffer — keeps the most recent PDF/file per phone so we can upload to Drive on demand
# ---------------------------------------------------------------------------
import time as _time
_doc_buffer: dict[str, dict] = {}

def store_doc(phone: str, filename: str, content_bytes: bytes, mime: str, text: str, pages: int = 0):
    _doc_buffer[phone] = {
        "filename": filename,
        "bytes":    content_bytes,
        "mime":     mime,
        "text":     text,
        "pages":    pages,
        "ts":       _time.time(),
    }
    # Cleanup entries older than 1 hour
    cutoff = _time.time() - 3600
    for k in list(_doc_buffer.keys()):
        if _doc_buffer[k]["ts"] < cutoff:
            _doc_buffer.pop(k, None)

def get_doc(phone: str) -> Optional[dict]:
    return _doc_buffer.get(phone)

# ---------------------------------------------------------------------------
# Chat logging
# ---------------------------------------------------------------------------
def log_chat(phone: str, direction: str, message: str, msg_type: str = "text"):
    if SUPABASE_OK:
        try:
            sb.save_message(phone, direction, message, msg_type)
        except Exception as e:
            print(f"Chat log error: {e}")

# ---------------------------------------------------------------------------
# WhatsApp media downloader
# ---------------------------------------------------------------------------
def download_wa_media(media_id: str) -> tuple[bytes, str]:
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    info    = requests.get(f"https://graph.facebook.com/v19.0/{media_id}", headers=headers, timeout=15).json()
    content = requests.get(info["url"], headers=headers, timeout=30).content
    return content, info.get("mime_type", "application/octet-stream")

# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
def handle_text(phone: str, text: str) -> str:
    return ask_claude(phone, [{"type": "text", "text": text}])

def handle_image(phone: str, msg: dict) -> str:
    try:
        content, mime = download_wa_media(msg["image"]["id"])
        if mime not in ("image/jpeg","image/png","image/gif","image/webp"):
            mime = "image/jpeg"
        caption = (msg["image"].get("caption","") or "").strip()
        instruction = caption if caption else "Briefly describe what you see (1 line) then ask what they want done with it (extract text, explain, identify, etc). DO NOT do a long analysis until they ask."
        parts = [
            {"type":"image","source":{"type":"base64","media_type":mime,"data":base64.b64encode(content).decode()}},
            {"type":"text","text": instruction},
        ]
        return ask_claude(phone, parts)
    except Exception as e:
        return "❌ Couldn't read the image. Please try sending it again."

def handle_document(phone: str, msg: dict) -> str:
    doc      = msg["document"]
    filename = doc.get("filename","document")
    mime     = doc.get("mime_type","")
    caption  = (doc.get("caption","") or "").strip()
    try:
        content, mime_type = download_wa_media(doc["id"])
        if "pdf" in mime.lower() or filename.lower().endswith(".pdf"):
            if not PDF_OK:
                return "📄 PDF support not available right now."
            # Fast extraction — max 15 pages, stop early once we have enough text
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                page_count = len(pdf.pages)
                max_pages  = min(page_count, 15)  # never read more than 15 pages
                chunks = []
                char_budget = 4000  # stop extracting once we have this much text
                for i, p in enumerate(pdf.pages[:max_pages]):
                    t = (p.extract_text() or "").strip()
                    if t:
                        chunks.append(f"[Page {i+1}]\n{t}")
                    if sum(len(c) for c in chunks) >= char_budget:
                        break
            text = "\n\n".join(chunks)
            if not text.strip():
                return f"📄 *{filename}* — couldn't extract text. It may be a scanned/image PDF."
            # Buffer the original file bytes so we can upload to Drive on demand
            store_doc(phone, filename, content, mime or "application/pdf", text, page_count)
            user_instruction = caption if caption else (
                "Owner sent a PDF. Show 4 button options for what to do — use send_button_menu with body "
                f"\"📄 *{filename}* ({page_count} pages) — what would you like?\" "
                "and buttons: '💾 Save to Drive', '📝 Summarize', '⚡ Key Actions'. "
                "After they choose, do that action."
            ) if is_owner(phone) else (
                "User sent a PDF. Briefly say what it is in 1 line, then ask what they want done with it. Do NOT summarize unless they ask."
            )
            parts = [{"type":"text","text":f"📄 PDF received: *{filename}* ({page_count} pages, showing first {max_pages})\n\nContent preview:\n{text[:4000]}\n\n---\n{user_instruction}"}]
            return ask_claude(phone, parts)
        elif "image" in mime.lower():
            if mime_type not in ("image/jpeg","image/png","image/gif","image/webp"):
                mime_type = "image/jpeg"
            user_instruction = caption if caption else "Briefly describe what this image shows (1 sentence) then ask what they want done with it."
            parts = [
                {"type":"image","source":{"type":"base64","media_type":mime_type,"data":base64.b64encode(content).decode()}},
                {"type":"text","text":f"📎 Document image: *{filename}*\n\n{user_instruction}"},
            ]
            return ask_claude(phone, parts)
        elif "text" in mime.lower():
            text  = content.decode("utf-8", errors="ignore")[:5000]
            user_instruction = caption if caption else "Briefly describe what this file contains and ask what they want done with it."
            parts = [{"type":"text","text":f"📄 Text file: *{filename}*\n\n{text}\n\n---\n{user_instruction}"}]
            return ask_claude(phone, parts)
        else:
            return f"📎 Got *{filename}* but I can't read this file type yet (supported: PDF, images, text)."
    except Exception as e:
        return f"❌ Couldn't read the document. Please try sending it again."

def handle_audio(phone: str, msg: dict) -> str:
    if not WHISPER_OK:
        return "🎤 Voice note received!\n\nTo enable transcription, add *OPENAI_API_KEY* to your .env\n\nGet one free at openai.com/api"
    try:
        content, _ = download_wa_media(msg["audio"]["id"])
        audio_file      = io.BytesIO(content)
        audio_file.name = "voice.ogg"
        transcript = oai_client.audio.transcriptions.create(model="whisper-1", file=audio_file, response_format="text")
        transcribed = transcript.strip()
        print(f"🎤 Transcribed: {transcribed}")
        log_chat(phone, "IN", f"[Voice] {transcribed}", "audio")
        reply = ask_claude(phone, [{"type":"text","text":f"[Voice note]: {transcribed}"}])
        return f"🎤 _{transcribed}_\n\n{wa_format(reply)}"
    except Exception as e:
        return f"❌ Couldn't transcribe: {e}"

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "add_task",
        "description": "Add a task to the task manager.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name":   {"type":"string"},
                "description":{"type":"string","default":""},
                "category":   {"type":"string","default":"General"},
                "priority":   {"type":"string","description":"🔴 High | 🟡 Medium | 🟢 Low","default":"🟡 Medium"},
                "due_date":   {"type":"string","default":""},
                "status":     {"type":"string","default":"📋 To Do"},
            },
            "required": ["task_name"],
        },
    },
    {
        "name": "get_tasks",
        "description": "List tasks. Filter by status (To Do/In Progress/Done), period (today/week/month/overdue), or search by name. Use this for ALL task queries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter":{"type":"string","default":"","description":"To Do | In Progress | Review | Done | Cancelled"},
                "search":       {"type":"string","default":""},
                "period":       {"type":"string","default":"","description":"today | week | month | overdue (filters by due_date)"},
            },
            "required": [],
        },
    },
    {
        "name": "update_task_status",
        "description": "Update task stage by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id":   {"type":"integer"},
                "new_status":{"type":"string"},
            },
            "required": ["task_id","new_status"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Schedule a meeting on Google Calendar and get a Google Meet link. Do NOT ask for attendees or emails — just book it and return the meet_link to share.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":         {"type":"string"},
                "start_datetime":{"type":"string","description":"Format: YYYY-MM-DDTHH:MM:SS (24hr, no timezone)"},
                "end_datetime":  {"type":"string","description":"Format: YYYY-MM-DDTHH:MM:SS (24hr, no timezone)"},
                "description":   {"type":"string","default":""},
            },
            "required": ["title","start_datetime","end_datetime"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "Show upcoming meetings/events. Returns events with meet_link (Google Meet) and link (calendar). When user asks for meeting link, share meet_link.",
        "input_schema": {
            "type": "object",
            "properties": {"max_results":{"type":"integer","default":10}},
            "required": [],
        },
    },
    {
        "name": "save_document_to_drive",
        "description": "Upload the most recently received document/PDF to the user's Google Drive (folder: 'WhatsApp Documents') and log it to the Documents sheet. Returns a shareable Drive link. Call this when the owner clicks 'Save to Drive' button or asks to save a document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {"type":"string","default":"","description":"Optional short note about the document for the sheet log"},
            },
            "required": [],
        },
    },
    {
        "name": "send_button_menu",
        "description": "Send WhatsApp button menu for quick actions (max 3 buttons).",
        "input_schema": {
            "type": "object",
            "properties": {
                "body":   {"type":"string"},
                "buttons":{"type":"array","items":{"type":"object","properties":{"id":{"type":"string"},"title":{"type":"string"}}}},
            },
            "required": ["body","buttons"],
        },
    },
]

def execute_tool(name: str, inp: dict[str, Any], phone: str) -> str:
    if name == "send_button_menu":
        send_buttons(phone, inp["body"], inp["buttons"])
        # Log the button message to chat history so it shows in dashboard
        btn_labels = " | ".join(b.get("title","") for b in inp.get("buttons",[]))
        log_chat(phone, "OUT", f"🔘 {inp['body']}\n[{btn_labels}]", "button")
        return json.dumps({"success": True})

    if name == "save_document_to_drive":
        if not GOOGLE_OK:
            return json.dumps({"success": False, "error": "Google Drive not configured"})
        doc = get_doc(phone)
        if not doc:
            return json.dumps({"success": False, "error": "No recent document found in buffer (received >1hr ago or never sent)"})
        try:
            up = gs.upload_to_drive(doc["bytes"], doc["filename"], doc["mime"])
            if not up.get("success"):
                return json.dumps(up)
            # Log to Documents sheet
            try:
                gs.append_document_to_sheet(
                    filename=doc["filename"], drive_link=up["link"],
                    doc_type="PDF" if "pdf" in doc["mime"].lower() else "File",
                    pages=doc.get("pages", 0), notes=inp.get("notes",""),
                    uploaded_by=phone,
                )
            except Exception as se:
                print(f"⚠️ Doc sheet log failed (non-fatal): {se}")
            return json.dumps({"success": True, "link": up["link"], "filename": doc["filename"]})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    # Tasks — use Supabase if available, else Google Sheets
    if name in ("add_task","get_tasks","update_task_status"):
        svc = sb if SUPABASE_OK else (gs if GOOGLE_OK else None)
        if not svc:
            return json.dumps({"error":"No storage configured (need Supabase or Google)"})
        try:
            if name == "add_task":
                # Save to PRIMARY (Supabase if available, else Google)
                primary_res = svc.add_task(
                    task_name=inp["task_name"], description=inp.get("description",""),
                    category=inp.get("category","General"), priority=inp.get("priority","🟡 Medium"),
                    due_date=inp.get("due_date",""), status=inp.get("status","📋 To Do"),
                )
                # ALSO mirror to Google Sheets if both are enabled (so user can see in their sheet)
                if SUPABASE_OK and GOOGLE_OK:
                    try:
                        sheet_res = gs.add_task(
                            task_name=inp["task_name"], description=inp.get("description",""),
                            category=inp.get("category","General"), priority=inp.get("priority","🟡 Medium"),
                            due_date=inp.get("due_date",""), status=inp.get("status","📋 To Do"),
                        )
                        if not sheet_res.get("success"):
                            print(f"⚠️ Tasks sheet mirror failed: {sheet_res.get('error')}")
                        else:
                            print(f"✅ Mirrored task to Tasks sheet")
                    except Exception as me:
                        print(f"⚠️ Tasks sheet mirror exception: {me}")
                return json.dumps(primary_res)
            elif name == "get_tasks":
                if SUPABASE_OK:
                    res = svc.get_tasks(status_filter=inp.get("status_filter",""), search=inp.get("search",""))
                else:
                    res = svc.get_tasks(status_filter=inp.get("status_filter",""))
                # Filter by period if requested
                period = (inp.get("period","") or "").lower().strip()
                if period and res.get("tasks"):
                    from datetime import date, timedelta
                    today = date.today()
                    filtered = []
                    for t in res["tasks"]:
                        dd = (t.get("due_date") or "").strip()
                        if not dd:
                            if period == "today": continue
                            filtered.append(t); continue
                        try:
                            td = datetime.strptime(dd[:10], "%Y-%m-%d").date()
                        except: continue
                        if period == "today" and td == today: filtered.append(t)
                        elif period == "week" and today <= td <= today + timedelta(days=7): filtered.append(t)
                        elif period == "month" and td.year == today.year and td.month == today.month: filtered.append(t)
                        elif period == "overdue" and td < today and "Done" not in t.get("status",""): filtered.append(t)
                    res["tasks"] = filtered
                    res["period"] = period
                return json.dumps(res)
            elif name == "update_task_status":
                primary_res = svc.update_task_status(inp["task_id"], inp["new_status"])
                # Mirror to sheets if both stores active
                if SUPABASE_OK and GOOGLE_OK:
                    try:
                        gs.update_task_status(inp["task_id"], inp["new_status"])
                    except Exception as me:
                        print(f"⚠️ Tasks sheet mirror update failed: {me}")
                return json.dumps(primary_res)
        except Exception as e:
            return json.dumps({"error": str(e)})

    # Calendar — Google only
    if not GOOGLE_OK:
        return json.dumps({"error": "Google Calendar not configured. Tell user calendar service is unavailable."})
    try:
        if name == "create_calendar_event":
            print(f"📅 Creating event: {inp.get('title')} {inp.get('start_datetime')} → {inp.get('end_datetime')}")
            result = gs.create_calendar_event(
                title=inp["title"], start_datetime=inp["start_datetime"],
                end_datetime=inp["end_datetime"], description=inp.get("description",""),
                attendees=[],   # never send invites or emails
                location="",
            )
            print(f"📅 Calendar result: {result}")
            if result.get("success"):
                try:
                    sheet_res = gs.append_meeting_to_sheet(
                        title=result["title"], start_datetime=result["start"],
                        end_datetime=result["end"],
                        description=inp.get("description",""),
                        event_link=result.get("event_link",""),
                        meet_link=result.get("meet_link",""),
                    )
                    if not sheet_res.get("success"):
                        print(f"⚠️ Meetings sheet log failed: {sheet_res.get('error')}")
                        result["sheet_warning"] = sheet_res.get("error")
                    else:
                        print(f"✅ Logged to Meetings sheet (row {sheet_res.get('meeting_num')})")
                except Exception as se:
                    import traceback
                    print(f"⚠️ Meetings sheet log exception:")
                    print(traceback.format_exc())
                    result["sheet_warning"] = str(se)
            return json.dumps(result)
        elif name == "list_calendar_events":
            return json.dumps(gs.list_calendar_events(max_results=inp.get("max_results",10)))
    except Exception as e:
        import traceback
        print(f"⚠️ Calendar tool error: {e}")
        print(traceback.format_exc())
        return json.dumps({"error": str(e), "hint": "Check service_account.json shared with calendar"})

    return json.dumps({"error": f"Unknown tool: {name}"})

# ---------------------------------------------------------------------------
# System prompts — Owner vs Client
# ---------------------------------------------------------------------------
_TODAY = datetime.now().strftime("%A, %d %B %Y")

# OWNER PROMPT — full access, this is Daud himself
OWNER_SYSTEM_PROMPT = f"""You are Muhammad Daud Zia's personal AI assistant on WhatsApp. Today: {_TODAY}. Timezone: Pakistan Standard Time (PKT, UTC+5).

PERSONALITY: Warm, friendly, professional — like a trusted human assistant who knows Daud well. Use natural conversational language, not curt one-word replies. Be polite and helpful, never blunt or robotic. Match his energy — if he writes casually, reply casually but warmly.

LANGUAGE: Match whatever Daud writes — English, Urdu, Roman Urdu, Hinglish. Detect per message.

TONE EXAMPLES:
✅ Good: "Sure! Who is the call with?" "Got it — booking now." "Done! Your meeting with Amjad is set for tomorrow at 8 PM PKT."
❌ Bad:  "What do you need?" "Who's the call with?" "Done." "What's next?"

CORE RULES:
1. Execute immediately — "show tasks" → call get_tasks right away. But still phrase it warmly: "Here are your tasks for today..." not just dump data.
2. Replies should feel human — 2-6 lines, conversational, not robotic.
3. NEVER use empty/single-emoji replies (no standalone ✅, ✔️, 👍 messages). If you call send_button_menu, ALSO include a friendly intro text in the body — don't send empty followups.
4. Confirm done tasks warmly — "Added! ✅ Get gym membership is on your list for tomorrow at 5 PM."
5. If something fails, explain plainly and offer to retry.
6. ALWAYS confirm timezone is Pakistan Time (PKT) for meetings.

BUTTONS — use send_button_menu tool when:
• Greeting/first message → body: "Hi Daud! 👋 What can I help you with today?", buttons: "📋 Tasks", "📅 Calendar", "➕ Add Task"
• When offering 2-3 clear next-step options
The body text must be friendly and complete — not just "Pick one". Max 3 buttons, 20 chars each.

TASK QUERIES:
- "today/daily" → get_tasks(period="today")
- "this week" → get_tasks(period="week")
- "this month" → get_tasks(period="month")
- "overdue" → get_tasks(period="overdue")
- "all tasks" → get_tasks() no filter

TASK LIST FORMAT:
*Today Tasks (3)*
🔴 *Client meeting prep* · To Do
🟡 *Review proposal* · In Progress
🟢 *Send invoice* · Done
Priority: 🔴 High · 🟡 Medium · 🟢 Low
Stages: 📋 To Do → 🔄 In Progress → 👀 Review → ✅ Done

CALENDAR BOOKING FLOW:
Collect these ONE AT A TIME (not all at once):

STEP 1 — Who: "Sure! Who is the call with?"
STEP 2 — Title/topic (if not given): "What is the meeting about?"
STEP 3 — Date/time (if not given): "What time works for you?"
STEP 4 — Duration: USE send_button_menu tool here!
   body: "How long should the meeting be?"
   buttons: "⏱️ 30 minutes", "🕐 1 hour"

If Daud already provides info upfront (e.g. "book 1hr call with Ahmad tomorrow 6pm about crypto"), do not ask again — just confirm and book directly.

NEVER ask for email or attendees. ALL times are Pakistan Standard Time (PKT, UTC+5).

Call create_calendar_event with EXACT format:
- title: "[topic] - [name]" (e.g. "Project review - Ahmad")
- start_datetime: PASS USER TIME EXACTLY AS STATED (already in PKT). Do NOT convert to UTC. Do NOT add/subtract hours.
  Format: "YYYY-MM-DDTHH:MM:SS" in 24-hour, NO timezone suffix.
  If Daud says "11 AM" → pass "YYYY-MM-DDT11:00:00" (NOT 06:00:00).
  If Daud says "6 PM" → pass "YYYY-MM-DDT18:00:00" (NOT 13:00:00).
- end_datetime: start + chosen duration:
  → 30 min: "11:00:00" → "11:30:00"
  → 1 hour: "11:00:00" → "12:00:00"
- Today is {_TODAY}. Calculate correct date from "tomorrow", "Monday", etc.
- Google receives Asia/Karachi timezone automatically — your job is just to pass HH:MM as the user said.

SUCCESS — reply with this PROFESSIONAL FORMAT:

📅 *Meeting Confirmed!*

👤 *With:* [name]
📌 *Topic:* [title]
📆 *Date:* [Day, DD Month] (e.g., Tuesday, 28 April)
🕐 *Time:* [time] PKT (e.g., 8:00 PM PKT)
⏱️ *Duration:* [30 minutes / 1 hour]

🔗 *Join Meeting:* [meet_link]

_See you there! 👋_

SUCCESS but meet_link empty: same format with "_Meet link will generate shortly._"
FAILURE: share the EXACT error message from create_calendar_event response.
DO NOT say "technical issue", "glitching", "connection issue", or "will be restored" — those are LIES.
DO NOT offer to add as a task instead — just tell Daud what failed.
Format: "Couldn't book the meeting. Error: <paste literal error from tool response>"
NEVER invent meet links.
NEVER claim the calendar is broken when you don't know — share the actual error so Daud can debug.

DOCUMENT/PDF HANDLING (owner only):
When Daud sends a PDF, ALWAYS use send_button_menu first with these 3 action buttons:
  body: "📄 *<filename>* (<N> pages) — what would you like?"
  buttons: "💾 Save to Drive", "📝 Summarize", "⚡ Key Actions"

Then act on his choice:
- "💾 Save to Drive" or any save request → call save_document_to_drive tool. After success, reply:
  "✅ Saved to Drive!

📄 *[filename]*
🔗 [drive_link]

_Logged in your Documents sheet._"
- "📝 Summarize" → write a clean 4-6 line summary in WhatsApp format with bullets
- "⚡ Key Actions" → list 3-5 actionable items extracted from the PDF (use bullets)
- Any free-text question about the PDF → answer using PDF context

IMAGES: Briefly describe in 1 line, ask what to do. Do not auto-analyze.

DON'T:
- Say "What is next?" or "Anything else?" or "How can I help further?" — just stop after confirming.
- Apologize more than once for the same thing
- Use more than 3 emojis per message
- Add unnecessary line breaks"""

# CLIENT PROMPT — restricted, for clients/customers/team members
CLIENT_SYSTEM_PROMPT = f"""You are the professional AI assistant for Muhammad Daud Zia — a business manager. Today: {_TODAY}.

YOUR ROLE: You represent Daud to his clients, customers, and team members. You help with:
- Booking a call or meeting with Daud
- Questions about Daud or his services
- General conversation and assistance

YOU CANNOT: Access Daud's personal tasks, private notes, or internal business data. If asked, politely decline.

PERSONALITY: Professional, warm, human. Like a real receptionist. Natural conversation — not robotic. Never say "What is next?" or "How can I assist you further?"

LANGUAGE: Match what the person writes — English, Urdu, Roman Urdu, etc.

GREETING (hi/hello/salam/hey): Warmly introduce yourself and offer buttons.
Use send_button_menu with: body="Hi! 👋 I am Daud's assistant. How can I help you today?", buttons: "📅 Book a Meeting", "ℹ️ Learn More", "💬 Send a Message"

MEETING BOOKING (warm, professional, step-by-step flow):
Collect these 4 things ONE AT A TIME (do not ask all at once):

STEP 1 — Name (if not already given):
"May I have your name, please? 🙏"

STEP 2 — Meeting topic/title:
"What is the meeting about? (e.g., Project discussion, Crypto consultation)"

STEP 3 — Preferred date and time:
"What date and time works best for you? (Pakistan time / PKT)"

STEP 4 — Duration: USE send_button_menu tool here!
body: "How long should the meeting be?"
buttons: "⏱️ 30 minutes", "🕐 1 hour"

Once you have all 4 → call create_calendar_event:
- title: use the meeting topic + name (e.g. "Crypto consultation - Ahmad")
- start_datetime: "2025-04-28T14:00:00" (YYYY-MM-DDTHH:MM:SS, 24-hour, no timezone)
- end_datetime: start + 30min OR start + 1hr based on duration choice
  → 30 min: 14:00 → 14:30
  → 1 hour: 14:00 → 15:00
- Today is {_TODAY}. PKT timezone. Never ask for email.

After booking — reply with this PROFESSIONAL FORMAT:

📅 *Meeting Confirmed with Daud!*

👤 *Booked By:* [name]
📌 *Topic:* [title]
📆 *Date:* [Day, DD Month]
🕐 *Time:* [time] PKT
⏱️ *Duration:* [30 minutes / 1 hour]

🔗 *Join Google Meet:* [meet_link]

_Looking forward to speaking with you! 🙏_

If meet_link empty: replace link line with "_Meet link will be ready shortly._"
NEVER ask for email addresses. NEVER ask everything at once — go step by step.

IF ASKED ABOUT TASKS/PRIVATE INFO: "That is Daud's private workspace — I do not have access to that. Can I help you book a meeting instead?"

KEEP IT: Short, friendly, human, professional."""

def get_system_prompt(phone: str) -> str:
    return OWNER_SYSTEM_PROMPT if is_owner(phone) else CLIENT_SYSTEM_PROMPT
# ---------------------------------------------------------------------------
# WhatsApp formatting helpers
# ---------------------------------------------------------------------------
def wa_format(text: str) -> str:
    """Convert standard markdown to WhatsApp-safe formatting."""
    if not text: return text
    # **bold** or __bold__ → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__',     r'_\1_', text, flags=re.DOTALL)
    # ### Heading → *Heading*
    text = re.sub(r'(?m)^#{1,6}\s+(.+)$', r'*\1*', text)
    # Normalize bullet points
    text = re.sub(r'(?m)^\s*[-–•]\s+', '• ', text)
    # Collapse 3+ blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ---------------------------------------------------------------------------
# Claude agent loop
# ---------------------------------------------------------------------------
def _block_type(b):
    """Get block type whether it's a dict or anthropic object."""
    if isinstance(b, dict): return b.get("type")
    return getattr(b, "type", None)

def _serialize_content(content) -> list:
    """Convert Anthropic SDK content blocks to plain JSON-serializable dicts."""
    result = []
    for block in (content if isinstance(content, list) else []):
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "type"):
            t = block.type
            if t == "text":
                result.append({"type": "text", "text": block.text})
            elif t == "tool_use":
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif t == "tool_result":
                result.append({"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content})
            else:
                result.append({"type": t})
        else:
            result.append({"type": "text", "text": str(block)})
    return result

def sanitize_history(history: list) -> list:
    """Drop any assistant message with tool_use that doesn't have a matching tool_result next.
    Also drop user messages with tool_result whose tool_use was already dropped."""
    cleaned = []
    i = 0
    while i < len(history):
        msg = history[i]
        role = msg.get("role")
        content = msg.get("content", [])
        if not isinstance(content, list): content = []

        if role == "assistant":
            has_tool_use = any(_block_type(b) == "tool_use" for b in content)
            if has_tool_use:
                # Need NEXT message to be a user with tool_result
                nxt = history[i+1] if i+1 < len(history) else None
                if nxt and nxt.get("role") == "user":
                    nxt_content = nxt.get("content", [])
                    if isinstance(nxt_content, list) and any(_block_type(b) == "tool_result" for b in nxt_content):
                        cleaned.append(msg)
                        cleaned.append(nxt)
                        i += 2
                        continue
                # Orphaned tool_use — skip this assistant message
                print(f"⚠️  Dropping orphaned tool_use at index {i}")
                i += 1
                continue
        elif role == "user":
            # Drop user messages that are PURELY tool_results (orphaned)
            if content and all(_block_type(b) == "tool_result" for b in content):
                print(f"⚠️  Dropping orphaned tool_result at index {i}")
                i += 1
                continue
        cleaned.append(msg)
        i += 1
    return cleaned

def safe_trim(history: list, max_msgs: int = 20) -> list:
    """Trim history but never split a tool_use/tool_result pair."""
    if len(history) <= max_msgs: return history
    trimmed = history[-max_msgs:]
    # If the first kept message is a user with tool_result, drop it (no preceding tool_use)
    while trimmed and trimmed[0].get("role") == "user":
        c = trimmed[0].get("content", [])
        if isinstance(c, list) and c and all(_block_type(b) == "tool_result" for b in c):
            trimmed = trimmed[1:]
        else:
            break
    return trimmed

def ask_claude(phone: str, content: list) -> str:
    # Owner gets all tools; clients only get calendar + button tools (NOT Drive — that's private)
    owner = is_owner(phone)
    active_tools = TOOLS if owner else [t for t in TOOLS if t["name"] in ("create_calendar_event", "list_calendar_events", "send_button_menu")]
    system = get_system_prompt(phone)

    history = load_history(phone)
    history = sanitize_history(history)
    history.append({"role": "user", "content": content})

    while True:
        try:
            response = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=system,
                tools=active_tools,
                messages=safe_trim(history, 20),
            )
        except anthropic.BadRequestError as e:
            # Conversation corruption — reset and try once more with just the new message
            print(f"⚠️  Claude API error, resetting history: {e}")
            history = [{"role": "user", "content": content}]
            _mem_cache[phone] = []
            if SUPABASE_OK: sb.save_conversation(phone, [])
            response = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=system,
                tools=active_tools,
                messages=history,
            )
        text, tool_calls = "", []
        for block in response.content:
            if block.type == "text":       text += block.text
            elif block.type == "tool_use": tool_calls.append(block)

        history.append({"role": "assistant", "content": _serialize_content(response.content)})

        if response.stop_reason == "end_turn" or not tool_calls:
            save_history(phone, history)
            return wa_format(text)  # may be empty — caller will skip sending

        tool_results = []
        for tool in tool_calls:
            print(f"🔧 {tool.name}({json.dumps(tool.input)[:60]})")
            result = execute_tool(tool.name, tool.input, phone)
            tool_results.append({"type":"tool_result","tool_use_id":tool.id,"content":result})
        history.append({"role":"user","content":tool_results})

# ---------------------------------------------------------------------------
# Deduplication — Meta retries webhook if response > 10s, this prevents double processing
# ---------------------------------------------------------------------------
_processed_ids: OrderedDict[str, float] = OrderedDict()
_dedup_lock = threading.Lock()
def is_duplicate(msg_id: str) -> bool:
    if not msg_id: return False
    now = datetime.utcnow().timestamp()
    with _dedup_lock:
        # Clean entries older than 10 minutes
        cutoff = now - 600
        for k in list(_processed_ids.keys()):
            if _processed_ids[k] < cutoff: _processed_ids.pop(k, None)
            else: break
        if msg_id in _processed_ids: return True
        _processed_ids[msg_id] = now
        # Cap at 1000 entries
        while len(_processed_ids) > 1000: _processed_ids.popitem(last=False)
    return False

# ---------------------------------------------------------------------------
# WhatsApp senders
# ---------------------------------------------------------------------------
def mark_read_and_typing(message_id: str):
    """Mark message as read and show typing indicator."""
    if not message_id: return
    headers = {"Authorization":f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"}
    payload = {
        "messaging_product":"whatsapp",
        "status":"read",
        "message_id":message_id,
        "typing_indicator":{"type":"text"},
    }
    try:
        requests.post(GRAPH_URL, headers=headers, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ Typing indicator failed: {e}")

def send_text(to: str, text: str):
    _wa_post(to, {"type":"text","text":{"body":text}})

def send_buttons(to: str, body: str, buttons: list[dict]):
    _wa_post(to, {
        "type":"interactive",
        "interactive":{
            "type":"button","body":{"text":body},
            "action":{"buttons":[{"type":"reply","reply":{"id":b["id"],"title":b["title"][:20]}} for b in buttons[:3]]},
        },
    })

def _wa_post(to: str, extra: dict):
    headers = {"Authorization":f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"}
    r = requests.post(GRAPH_URL, headers=headers, json={"messaging_product":"whatsapp","to":to,**extra}, timeout=10)
    print(f"📤 [{r.status_code}] → {to}")
    if r.status_code != 200: print(f"   ⚠️ {r.text[:200]}")

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

@app.route("/")
def home():
    return send_from_directory(".", "dashboard.html")

@app.route("/dashboard.css")
def css():
    return send_from_directory(".", "dashboard.css")

def process_message_async(msg: dict, from_num: str, msg_type: str):
    """Process message in background thread so webhook can return 200 quickly."""
    try:
        msg_id = msg.get("id","")
        # Show typing indicator
        mark_read_and_typing(msg_id)

        if msg_type == "text":
            ut = msg["text"]["body"]
            log_chat(from_num,"IN",ut,"text")
            reply = handle_text(from_num, ut)
        elif msg_type == "image":
            cap = msg['image'].get('caption','').strip()
            log_chat(from_num,"IN",f"[Image] {cap}","image")
            reply = handle_image(from_num, msg)
        elif msg_type == "document":
            fn = msg['document'].get('filename','document')
            log_chat(from_num,"IN",f"[Doc] {fn}","document")
            reply = handle_document(from_num, msg)
        elif msg_type == "audio":
            log_chat(from_num,"IN","[Voice note]","audio")
            reply = handle_audio(from_num, msg)
        elif msg_type == "interactive":
            iv = msg.get("interactive",{})
            if iv.get("type") == "button_reply":
                bt = iv["button_reply"]["title"]
                log_chat(from_num,"IN",bt,"button")
                reply = handle_text(from_num, bt)
            else: return
        elif msg_type == "sticker":
            reply = ask_claude(from_num,[{"type":"text","text":"User sent a sticker — reply briefly and warmly."}])
        else:
            reply = f"I received your {msg_type}. How can I help?"

        # Only send if there is real text — buttons/menus are sent directly via send_button_menu
        if reply and reply.strip() and reply.strip() not in ("✅", "✔️", "👍"):
            send_text(from_num, reply)
            log_chat(from_num,"OUT",reply,"text")
    except Exception as e:
        import traceback
        print(f"⚠️ Process error: {e}")
        print(traceback.format_exc())
        try: send_text(from_num, "Sorry, something went wrong. Please try again. 🙏")
        except: pass


@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        m,t,c = request.args.get("hub.mode",""), request.args.get("hub.verify_token",""), request.args.get("hub.challenge","")
        return (c,200) if m=="subscribe" and t==VERIFY_TOKEN else ("Forbidden",403)

    data = request.get_json(force=True, silent=True) or {}
    try:
        entry = data.get("entry", [])
        if not entry: return jsonify({"status":"ok"}), 200
        value = entry[0].get("changes", [{}])[0].get("value", {})
        if "messages" not in value: return jsonify({"status":"ok"}),200
        msg      = value["messages"][0]
        msg_id   = msg.get("id","")
        from_num = msg["from"]
        msg_type = msg.get("type","")

        # Deduplicate — Meta retries on slow responses
        if is_duplicate(msg_id):
            print(f"🔁 Duplicate {msg_type} from {from_num} — skipping")
            return jsonify({"status":"ok"}),200

        print(f"\n📩 [{msg_type}] from {from_num} (id={msg_id[:12]}...)")

        # Process in background — return 200 fast so Meta doesn't retry
        threading.Thread(target=process_message_async, args=(msg, from_num, msg_type), daemon=True).start()

    except Exception as e:
        import traceback
        print(f"⚠️ Webhook error: {e}")
        print(traceback.format_exc())
    return jsonify({"status":"ok"}),200

# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------
@app.route("/api/stats")
def api_stats():
    stats = {"google_enabled":GOOGLE_OK,"supabase_enabled":SUPABASE_OK,"whisper_enabled":WHISPER_OK}
    if SUPABASE_OK:
        try: stats.update(sb.get_stats())
        except Exception as e: stats["error"] = str(e)
    if GOOGLE_OK:
        try: stats["upcoming_events"] = len(gs.list_calendar_events(max_results=50).get("events",[]))
        except: pass
    return jsonify(stats)

@app.route("/api/tasks")
def api_tasks():
    svc = sb if SUPABASE_OK else (gs if GOOGLE_OK else None)
    if not svc: return jsonify({"success":False,"tasks":[],"error":"No storage configured"}),503
    status = request.args.get("status","")
    search = request.args.get("search","")
    if SUPABASE_OK: return jsonify(sb.get_tasks(status_filter=status, search=search))
    return jsonify(gs.get_tasks(status_filter=status, max_rows=100))

@app.route("/api/tasks/add", methods=["POST"])
def api_add_task():
    svc = sb if SUPABASE_OK else (gs if GOOGLE_OK else None)
    if not svc: return jsonify({"success":False,"error":"No storage configured"}),503
    a = request.args
    return jsonify(svc.add_task(
        task_name=a.get("task_name","Untitled"), description=a.get("description",""),
        category=a.get("category","General"), priority=a.get("priority","🟡 Medium"),
        due_date=a.get("due_date",""), status=a.get("status","📋 To Do"),
    ))

@app.route("/api/tasks/<int:task_id>/status", methods=["POST"])
def api_update_task(task_id):
    svc = sb if SUPABASE_OK else (gs if GOOGLE_OK else None)
    if not svc: return jsonify({"success":False,"error":"No storage configured"}),503
    return jsonify(svc.update_task_status(task_id, (request.get_json() or {}).get("status","")))

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_delete_task(task_id):
    if not SUPABASE_OK: return jsonify({"success":False,"error":"Supabase required"}),503
    return jsonify(sb.delete_task(task_id))

@app.route("/api/chats")
def api_chats():
    if not SUPABASE_OK: return jsonify({"success":False,"chats":[]}),503
    phone = request.args.get("phone","")
    return jsonify(sb.get_chat_history(phone=phone, limit=200))

@app.route("/api/contacts")
def api_contacts():
    if not SUPABASE_OK: return jsonify({"success":False,"contacts":[]}),503
    return jsonify(sb.get_contacts())

@app.route("/api/chats/<phone>", methods=["DELETE"])
def api_delete_chat(phone):
    if not SUPABASE_OK: return jsonify({"success":False,"error":"Supabase required"}),503
    # Also reset conversation memory
    _mem_cache.pop(phone, None)
    try: sb.save_conversation(phone, [])
    except: pass
    return jsonify(sb.delete_chat(phone))

@app.route("/api/memory/<phone>/reset", methods=["POST"])
def api_reset_memory(phone):
    """Clear Claude's conversation memory (but keep chat log)."""
    _mem_cache.pop(phone, None)
    if SUPABASE_OK:
        try: sb.save_conversation(phone, [])
        except Exception as e: return jsonify({"success":False,"error":str(e)}),500
    return jsonify({"success": True, "message": f"Memory cleared for {phone}"})

@app.route("/api/events")
def api_events():
    if not GOOGLE_OK: return jsonify({"success":False,"error":"Google not configured"}),503
    return jsonify(gs.list_calendar_events(max_results=20))

@app.route("/api/meetings")
def api_meetings():
    if not GOOGLE_OK: return jsonify({"success":False,"error":"Google not configured"}),503
    return jsonify(gs.get_sheet_meetings(max_rows=50))

# ---------------------------------------------------------------------------
# Daily Reminder Scheduler — 8AM Pakistan Time (PKT = UTC+5)
# ---------------------------------------------------------------------------
REMINDER_PHONE = os.environ.get("REMINDER_PHONE", "")  # set this in .env to your WhatsApp number

def send_daily_summary():
    if not REMINDER_PHONE or not SUPABASE_OK:
        return
    try:
        from datetime import date, timedelta
        today = date.today()
        all_tasks = sb.get_tasks(limit=200).get("tasks", [])
        todays, overdue = [], []
        for t in all_tasks:
            if "Done" in t.get("status","") or "Cancelled" in t.get("status",""): continue
            dd = (t.get("due_date") or "").strip()
            if not dd: continue
            try: td = datetime.strptime(dd[:10], "%Y-%m-%d").date()
            except: continue
            if td == today: todays.append(t)
            elif td < today: overdue.append(t)

        lines = [f"☀️ *Good morning!*", f"📅 {today.strftime('%A, %d %B %Y')}", ""]
        if todays:
            lines.append(f"🎯 *Today's Tasks ({len(todays)}):*")
            for t in todays[:8]:
                lines.append(f"• {t.get('task_name','')} {t.get('priority','')[:2]}")
            lines.append("")
        if overdue:
            lines.append(f"⏰ *Overdue ({len(overdue)}):*")
            for t in overdue[:5]:
                lines.append(f"• {t.get('task_name','')} (due {t.get('due_date','')})")
            lines.append("")
        if not todays and not overdue:
            lines.append("✅ No tasks for today. Enjoy! ☕")
        else:
            lines.append("_Reply 'tasks' to see all_")

        send_text(REMINDER_PHONE, "\n".join(lines))
        log_chat(REMINDER_PHONE, "OUT", "\n".join(lines), "text")
        print(f"📢 Daily summary sent to {REMINDER_PHONE}")
    except Exception as e:
        print(f"⚠️ Daily summary error: {e}")

_scheduler_started = False
def start_scheduler():
    global _scheduler_started
    if _scheduler_started: return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from pytz import timezone
        sched = BackgroundScheduler(timezone=timezone("Asia/Karachi"))
        sched.add_job(send_daily_summary, "cron", hour=8, minute=0)
        sched.start()
        _scheduler_started = True
        print("⏰ Daily reminder scheduler running (8:00 AM PKT)")
    except Exception as e:
        print(f"⚠️ Scheduler not started: {e} (run: pip install apscheduler pytz)")

# Start on import (for gunicorn/Render)
start_scheduler()

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT",5000))
    print("="*55)
    print("  WhatsApp AI Manager — Claude + Supabase")
    print("="*55)
    print(f"  Supabase : {'✅' if SUPABASE_OK else '⚠️  Disabled'}")
    print(f"  Google   : {'✅' if GOOGLE_OK else '⚠️  Disabled'}")
    print(f"  Whisper  : {'✅' if WHISPER_OK else '⚠️  No OPENAI_API_KEY'}")
    print("="*55)
    app.run(host="0.0.0.0", port=port, debug=False)
