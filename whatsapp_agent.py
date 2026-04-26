"""
Personal WhatsApp AI Manager — powered by Claude + Supabase
Supports: Text, Images, Documents, Voice Notes, Any Language
Memory: Supabase (persistent conversation history)
"""
import os, json, io, base64, threading, requests, anthropic
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime
from typing import Any
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

def save_history(phone: str, history: list):
    _mem_cache[phone] = history
    if SUPABASE_OK:
        sb.save_conversation(phone, history)

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
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                pages = pdf.pages
                page_count = len(pages)
                text = "\n\n".join(
                    f"[Page {i+1}]\n{(p.extract_text() or '').strip()}"
                    for i, p in enumerate(pages) if p.extract_text()
                )
            if not text.strip():
                return f"📄 *{filename}* — I couldn't extract text. This might be a scanned PDF (image-only)."
            # Pass content + filename + page count + caption so Claude can give brief intro
            user_instruction = caption if caption else "Briefly describe what this document is (1 sentence) then ask the user what they want done with it. DO NOT summarize unless they ask."
            parts = [{"type":"text","text":f"📄 PDF received: *{filename}* ({page_count} pages)\n\nContent preview:\n{text[:5000]}\n\n---\nUser instruction: {user_instruction}"}]
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
        return f"🎤 _{transcribed}_\n\n{reply}"
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
        "description": "Schedule a meeting or event on Google Calendar. Automatically creates a Google Meet link. Returns meet_link (share this with attendees) and event_link (calendar page).",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":         {"type":"string"},
                "start_datetime":{"type":"string"},
                "end_datetime":  {"type":"string"},
                "description":   {"type":"string","default":""},
                "attendees":     {"type":"array","items":{"type":"string"},"default":[]},
                "location":      {"type":"string","default":""},
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

    # Tasks — use Supabase if available, else Google Sheets
    if name in ("add_task","get_tasks","update_task_status"):
        svc = sb if SUPABASE_OK else (gs if GOOGLE_OK else None)
        if not svc:
            return json.dumps({"error":"No storage configured (need Supabase or Google)"})
        try:
            if name == "add_task":
                return json.dumps(svc.add_task(
                    task_name=inp["task_name"], description=inp.get("description",""),
                    category=inp.get("category","General"), priority=inp.get("priority","🟡 Medium"),
                    due_date=inp.get("due_date",""), status=inp.get("status","📋 To Do"),
                ))
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
                return json.dumps(svc.update_task_status(inp["task_id"], inp["new_status"]))
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
                attendees=inp.get("attendees") or [], location=inp.get("location",""),
            )
            print(f"📅 Calendar result: {result}")
            if result.get("success"):
                try:
                    gs.append_meeting_to_sheet(
                        title=result["title"], start_datetime=result["start"],
                        end_datetime=result["end"], attendees=", ".join(inp.get("attendees") or []),
                        description=inp.get("description",""), event_link=result.get("event_link",""),
                    )
                except Exception as se:
                    print(f"⚠️ Sheet log failed (non-fatal): {se}")
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
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are *Muhammad Daud Zia's Personal Assistant* on WhatsApp. Today: {datetime.utcnow().strftime('%A, %d %B %Y')}.

LANGUAGE: Reply in the SAME language the user writes (English, Urdu, Roman Urdu, Hinglish, etc). Detect each message individually.

CORE RULES:
1. Be DIRECT — execute requests, don't ask permission. "show tasks" → call get_tasks immediately.
2. NO repeated button menus. Buttons only on first contact OR when truly ambiguous.
3. Don't say "I'll do X" — just do it and confirm.
4. Keep replies SHORT (3-6 lines max). Long replies only when explaining things in detail.
5. Use clean formatting — single blank line between sections, no excessive emojis.

DOCUMENT/IMAGE HANDLING:
- When you receive an image or document, FIRST briefly say what it is (1 line), then ASK what they want done with it (summarize, extract key points, translate, answer questions, etc).
- Example: "Got it — *O1-FFA.pdf* (Financial Accounting study material, 3 pages). What would you like? Summary, key concepts, or specific topic explanation?"
- Wait for user's instruction. DON'T auto-summarize unless they say so.
- If they previously sent the same document, DON'T re-summarize — answer their new question using context.

TASK QUERIES:
- "today/daily" → get_tasks(period="today")
- "this week/weekly" → get_tasks(period="week")
- "this month/monthly" → get_tasks(period="month")
- "overdue" → get_tasks(period="overdue")
- "all" → get_tasks() with no filter

TASK LIST FORMAT (clean, no extra spacing):
*Today's Tasks (3)*

🔴 *Client meeting prep*
   Due today · 📋 To Do

🟡 *Review proposal*
   Due today · 🔄 In Progress

Stages: 📋 To Do → 🔄 In Progress → 👀 Review → ✅ Done
Priority: 🔴 High · 🟡 Medium · 🟢 Low

CALENDAR / MEETINGS:
- For booking: check list_calendar_events for conflicts → suggest 2-3 slots → after confirmation call create_calendar_event
- ALWAYS share `meet_link` (Google Meet), NOT `event_link`
- Format after booking:
  📅 *Meeting booked*
  🕐 Tomorrow, 27 April · 2:00 PM
  👥 Daud + you
  🔗 https://meet.google.com/abc-defg-hij

- If `meet_link` is empty (calendar error), say: "Meeting saved. I'll send the Meet link separately."
- Never invent meet links.

OUTGOING IDENTITY:
- You are Daud's assistant. When clients/visitors message, introduce: "Hi! I'm Daud's assistant — how can I help?"
- Schedule on his behalf using calendar tools. Confirm everything before booking.

DON'T:
- Apologize repeatedly
- Use phrases like "calendar mein issue hai" — just say "let me try again" and retry
- Send the same response twice
- Use more than 3 emojis per message
- Add unnecessary line breaks"""

# ---------------------------------------------------------------------------
# Claude agent loop
# ---------------------------------------------------------------------------
def _block_type(b):
    """Get block type whether it's a dict or anthropic object."""
    if isinstance(b, dict): return b.get("type")
    return getattr(b, "type", None)

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
    history = load_history(phone)
    history = sanitize_history(history)
    history.append({"role": "user", "content": content})

    while True:
        try:
            response = claude.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=safe_trim(history, 20),
            )
        except anthropic.BadRequestError as e:
            # Conversation corruption — reset and try once more with just the new message
            print(f"⚠️  Claude API error, resetting history: {e}")
            history = [{"role": "user", "content": content}]
            _mem_cache[phone] = []
            if SUPABASE_OK: sb.save_conversation(phone, [])
            response = claude.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            )
        text, tool_calls = "", []
        for block in response.content:
            if block.type == "text":       text += block.text
            elif block.type == "tool_use": tool_calls.append(block)

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_calls:
            save_history(phone, history)
            return text.strip() or "✅"

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

        if reply:
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
