"""
Personal WhatsApp AI Manager — powered by Claude
Supports: Text, Images, Documents, Voice Notes, Any Language
"""
import os, json, io, base64, requests, anthropic
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime
from typing import Any

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
# Google services
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
# OpenAI Whisper (voice transcription)
# ---------------------------------------------------------------------------
try:
    import openai as oai_lib
    oai_client = oai_lib.OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None
    WHISPER_OK = bool(OPENAI_KEY)
    print(f"{'✅' if WHISPER_OK else '⚠️ '} Whisper {'enabled' if WHISPER_OK else 'disabled (no OPENAI_API_KEY)'}")
except ImportError:
    oai_client = None
    WHISPER_OK = False
    print("⚠️  openai package not installed")

# ---------------------------------------------------------------------------
# PDF reader
# ---------------------------------------------------------------------------
try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False
    print("⚠️  pdfplumber not installed")

# ---------------------------------------------------------------------------
# Claude client & memory
# ---------------------------------------------------------------------------
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
conversations: dict[str, list] = {}

# ---------------------------------------------------------------------------
# WhatsApp Media Downloader
# ---------------------------------------------------------------------------
def download_wa_media(media_id: str) -> tuple[bytes, str]:
    """Download a media file from WhatsApp Cloud API."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    info = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        headers=headers, timeout=15
    ).json()
    url       = info["url"]
    mime_type = info.get("mime_type", "application/octet-stream")
    content   = requests.get(url, headers=headers, timeout=30).content
    return content, mime_type

# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
def handle_text(from_num: str, text: str) -> str:
    return ask_claude(from_num, [{"type": "text", "text": text}])


def handle_image(from_num: str, msg: dict) -> str:
    """Read and understand an image using Claude Vision."""
    media_id = msg["image"]["id"]
    caption  = msg["image"].get("caption", "")
    try:
        content, mime_type = download_wa_media(media_id)
        img_b64 = base64.b64encode(content).decode()
        # Supported MIME types for Claude vision
        if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            mime_type = "image/jpeg"

        parts = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": img_b64},
            },
            {
                "type": "text",
                "text": caption if caption else "What's in this image? Give a detailed and helpful response.",
            },
        ]
        return ask_claude(from_num, parts)
    except Exception as e:
        return f"❌ Couldn't read image: {e}"


def handle_document(from_num: str, msg: dict) -> str:
    """Read a document (PDF or image) and respond."""
    doc      = msg["document"]
    media_id = doc["id"]
    filename = doc.get("filename", "document")
    mime     = doc.get("mime_type", "")

    try:
        content, mime_type = download_wa_media(media_id)

        # ── PDF ──
        if "pdf" in mime.lower() or filename.lower().endswith(".pdf"):
            if not PDF_OK:
                return "📄 I received your PDF but pdfplumber is not installed. Add it to requirements.txt."
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n\n".join(
                    f"[Page {i+1}]\n{(p.extract_text() or '').strip()}"
                    for i, p in enumerate(pdf.pages)
                    if p.extract_text()
                )
            if not text.strip():
                return f"📄 I opened *{filename}* but couldn't extract any text. It might be a scanned image PDF."
            text = text[:6000]  # stay within token limits
            parts = [{"type": "text", "text": f"Document: *{filename}*\n\n{text}\n\nSummarise this and answer any questions about it."}]
            return ask_claude(from_num, parts)

        # ── Image sent as document ──
        elif "image" in mime.lower():
            img_b64 = base64.b64encode(content).decode()
            if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime_type = "image/jpeg"
            parts = [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": img_b64}},
                {"type": "text", "text": f"This is a document named '{filename}'. What does it say or show?"},
            ]
            return ask_claude(from_num, parts)

        # ── Text files ──
        elif "text" in mime.lower():
            text = content.decode("utf-8", errors="ignore")[:5000]
            parts = [{"type": "text", "text": f"File: *{filename}*\n\n{text}\n\nAnalyse this file."}]
            return ask_claude(from_num, parts)

        else:
            return f"📎 I received *{filename}* ({mime}) but can't read this file type yet.\n\nI support: PDFs, Images, Text files."

    except Exception as e:
        return f"❌ Couldn't read document: {e}"


def handle_audio(from_num: str, msg: dict) -> str:
    """Transcribe voice note with Whisper then reply with Claude."""
    media_id = msg["audio"]["id"]

    if not WHISPER_OK:
        return (
            "🎤 I received your voice note!\n\n"
            "To enable voice transcription, add your OpenAI API key:\n"
            "*OPENAI_API_KEY=sk-...* in your .env file\n\n"
            "Get one free at: openai.com/api"
        )

    try:
        content, mime_type = download_wa_media(media_id)

        # WhatsApp sends OGG/OPUS — Whisper accepts it
        audio_file      = io.BytesIO(content)
        audio_file.name = "voice.ogg"

        transcript = oai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="text",
        )
        transcribed = transcript.strip()
        print(f"🎤 Transcribed: {transcribed}")

        # Log and reply
        log_chat(from_num, "IN", f"[Voice] {transcribed}", "audio")
        parts = [{"type": "text", "text": f"[Voice note transcription]: {transcribed}"}]
        reply = ask_claude(from_num, parts)
        return f"🎤 _{transcribed}_\n\n{reply}"

    except Exception as e:
        return f"❌ Couldn't transcribe voice note: {e}"


def handle_sticker(from_num: str) -> str:
    return ask_claude(from_num, [{"type": "text", "text": "The user sent a sticker emoji. Reply playfully!"}])

# ---------------------------------------------------------------------------
# Claude agent loop (handles both text and multimodal content)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "add_task",
        "description": "Add a task to Google Sheets task manager.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name":   {"type": "string"},
                "description": {"type": "string", "default": ""},
                "category":    {"type": "string", "default": "General"},
                "priority":    {"type": "string", "description": "🔴 High | 🟡 Medium | 🟢 Low", "default": "🟡 Medium"},
                "due_date":    {"type": "string", "default": ""},
                "status":      {"type": "string", "default": "📋 To Do"},
            },
            "required": ["task_name"],
        },
    },
    {
        "name": "get_tasks",
        "description": "Get task list from Google Sheets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {"type": "string", "default": ""},
                "max_rows":      {"type": "integer", "default": 30},
            },
            "required": [],
        },
    },
    {
        "name": "update_task_status",
        "description": "Update task stage by task number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id":    {"type": "integer"},
                "new_status": {"type": "string"},
            },
            "required": ["task_id", "new_status"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Schedule a meeting or event on Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":          {"type": "string"},
                "start_datetime": {"type": "string"},
                "end_datetime":   {"type": "string"},
                "description":    {"type": "string", "default": ""},
                "attendees":      {"type": "array", "items": {"type": "string"}, "default": []},
                "location":       {"type": "string", "default": ""},
            },
            "required": ["title", "start_datetime", "end_datetime"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "Show upcoming schedule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "send_button_menu",
        "description": "Send WhatsApp button menu for quick actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "body":    {"type": "string"},
                "buttons": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}}},
                },
            },
            "required": ["body", "buttons"],
        },
    },
]


def execute_tool(name: str, inp: dict[str, Any], phone: str) -> str:
    if name == "send_button_menu":
        send_buttons(phone, inp["body"], inp["buttons"])
        return json.dumps({"success": True})

    if not GOOGLE_OK:
        return json.dumps({"error": "Google not configured (need service_account.json + GOOGLE_SPREADSHEET_ID)"})

    try:
        if name == "add_task":
            return json.dumps(gs.add_task(**{k: inp.get(k, v) for k, v in {
                "task_name": "", "description": "", "category": "General",
                "priority": "🟡 Medium", "due_date": "", "status": "📋 To Do"
            }.items() if k in inp or k == "task_name"} | {"task_name": inp["task_name"]}))
        elif name == "add_task":
            return json.dumps(gs.add_task(
                task_name=inp["task_name"],
                description=inp.get("description", ""),
                category=inp.get("category", "General"),
                priority=inp.get("priority", "🟡 Medium"),
                due_date=inp.get("due_date", ""),
                status=inp.get("status", "📋 To Do"),
            ))
        elif name == "get_tasks":
            return json.dumps(gs.get_tasks(status_filter=inp.get("status_filter", ""), max_rows=inp.get("max_rows", 30)))
        elif name == "update_task_status":
            return json.dumps(gs.update_task_status(inp["task_id"], inp["new_status"]))
        elif name == "create_calendar_event":
            result = gs.create_calendar_event(
                title=inp["title"], start_datetime=inp["start_datetime"],
                end_datetime=inp["end_datetime"], description=inp.get("description", ""),
                attendees=inp.get("attendees") or [], location=inp.get("location", ""),
            )
            if result.get("success"):
                gs.append_meeting_to_sheet(
                    title=result["title"], start_datetime=result["start"],
                    end_datetime=result["end"], attendees=", ".join(inp.get("attendees") or []),
                    description=inp.get("description", ""), event_link=result.get("event_link", ""),
                )
            return json.dumps(result)
        elif name == "list_calendar_events":
            return json.dumps(gs.list_calendar_events(max_results=inp.get("max_results", 10)))
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


SYSTEM_PROMPT = f"""You are a personal AI manager on WhatsApp. Today: {datetime.utcnow().strftime('%Y-%m-%d')}.

🌍 LANGUAGE RULE (MOST IMPORTANT):
- Detect the language of every message automatically
- ALWAYS reply in the EXACT same language the user writes in
- Arabic → reply Arabic | Urdu → reply Urdu | French → reply French | Spanish → reply Spanish
- Never switch languages unless the user switches first

📱 You handle:
- 💬 Text messages — chat, tasks, schedule
- 🖼️ Images — describe, analyse, read text in images
- 📄 Documents/PDFs — summarise, extract info, answer questions
- 🎤 Voice notes — respond to transcribed content naturally
- 🔘 Button menus — send for quick actions

📋 Task stages: 📋 To Do → 🔄 In Progress → 👀 Review → ✅ Done → ❌ Cancelled

📱 WhatsApp style:
- Keep replies SHORT (5-6 lines max)
- Use *bold* with asterisks, _italic_ with underscores
- Use emojis naturally
- For greetings, send a button menu with quick actions
- Always confirm actions clearly"""


def ask_claude(phone: str, content: list) -> str:
    """Run Claude agent loop with multimodal content."""
    if phone not in conversations:
        conversations[phone] = []

    conversations[phone].append({"role": "user", "content": content})
    history = conversations[phone][-20:]

    while True:
        response = client.messages.create(
            model="claude-opus-4-5",   # Use vision-capable model
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

        text       = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(block)

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_calls:
            conversations[phone] = history
            return text.strip() or "✅"

        tool_results = []
        for tool in tool_calls:
            print(f"🔧 {tool.name}({json.dumps(tool.input)[:60]})")
            result = execute_tool(tool.name, tool.input, phone)
            tool_results.append({"type": "tool_result", "tool_use_id": tool.id, "content": result})

        history.append({"role": "user", "content": tool_results})

# ---------------------------------------------------------------------------
# WhatsApp senders
# ---------------------------------------------------------------------------
def send_text(to: str, text: str):
    _wa_post(to, {"type": "text", "text": {"body": text}})

def send_buttons(to: str, body: str, buttons: list[dict]):
    _wa_post(to, {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                for b in buttons[:3]
            ]},
        },
    })

def _wa_post(to: str, extra: dict):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(GRAPH_URL, headers=headers,
                      json={"messaging_product": "whatsapp", "to": to, **extra}, timeout=10)
    print(f"📤 [{r.status_code}] → {to}")
    if r.status_code != 200:
        print(f"   ⚠️ {r.text[:200]}")

def log_chat(phone: str, direction: str, message: str, msg_type: str = "text"):
    if GOOGLE_OK:
        try:
            gs.append_chat_history(phone=phone, direction=direction, message=message, msg_type=msg_type)
        except Exception as e:
            print(f"Chat log error: {e}")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

@app.route("/")
def home():
    return send_from_directory(".", "dashboard.html")

@app.route("/dashboard.css")
def dashboard_css():
    return send_from_directory(".", "dashboard.css")

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode, token, challenge = (
            request.args.get("hub.mode", ""),
            request.args.get("hub.verify_token", ""),
            request.args.get("hub.challenge", ""),
        )
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Forbidden", 403

    data = request.get_json(silent=True) or {}
    try:
        value    = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return jsonify({"status": "ok"}), 200

        msg      = value["messages"][0]
        from_num = msg["from"]
        msg_type = msg.get("type", "")

        print(f"\n📩 [{msg_type}] from {from_num}")

        # ── Route by message type ──
        if msg_type == "text":
            user_text = msg["text"]["body"]
            log_chat(from_num, "IN", user_text, "text")
            reply = handle_text(from_num, user_text)

        elif msg_type == "image":
            caption = msg["image"].get("caption", "[image]")
            log_chat(from_num, "IN", f"[Image] {caption}", "image")
            send_text(from_num, "🖼️ Reading your image...")
            reply = handle_image(from_num, msg)

        elif msg_type == "document":
            filename = msg["document"].get("filename", "document")
            log_chat(from_num, "IN", f"[Document] {filename}", "document")
            send_text(from_num, f"📄 Reading *{filename}*...")
            reply = handle_document(from_num, msg)

        elif msg_type == "audio":
            log_chat(from_num, "IN", "[Voice note]", "audio")
            send_text(from_num, "🎤 Transcribing your voice note...")
            reply = handle_audio(from_num, msg)

        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                btn_title = interactive["button_reply"]["title"]
                log_chat(from_num, "IN", btn_title, "button")
                reply = handle_text(from_num, btn_title)
            else:
                return jsonify({"status": "ok"}), 200

        elif msg_type == "sticker":
            reply = handle_sticker(from_num)

        else:
            reply = f"I received your {msg_type} message but can't process that type yet 😊"

        if reply:
            send_text(from_num, reply)
            log_chat(from_num, "OUT", reply, "text")

    except Exception as e:
        print(f"⚠️ Webhook error: {e}")

    return jsonify({"status": "ok"}), 200

# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------
@app.route("/api/stats")
def api_stats():
    stats = {"google_enabled": GOOGLE_OK, "whisper_enabled": WHISPER_OK, "active_chats": len(conversations)}
    if GOOGLE_OK:
        try:
            tasks = gs.get_tasks(max_rows=200)
            all_t = tasks.get("tasks", [])
            stats.update({
                "total_tasks":     len(all_t),
                "todo_tasks":      len([t for t in all_t if "To Do"      in t.get("Status", "")]),
                "inprogress_tasks":len([t for t in all_t if "In Progress" in t.get("Status", "")]),
                "done_tasks":      len([t for t in all_t if "Done"        in t.get("Status", "")]),
                "upcoming_events": len(gs.list_calendar_events(max_results=50).get("events", [])),
            })
        except Exception as e:
            stats["error"] = str(e)
    return jsonify(stats)

@app.route("/api/tasks")
def api_tasks():
    if not GOOGLE_OK: return jsonify({"success": False, "error": "Google not configured"}), 503
    return jsonify(gs.get_tasks(status_filter=request.args.get("status", ""), max_rows=100))

@app.route("/api/tasks/add", methods=["POST"])
def api_add_task():
    if not GOOGLE_OK: return jsonify({"success": False, "error": "Google not configured"}), 503
    a = request.args
    return jsonify(gs.add_task(
        task_name=a.get("task_name", "Untitled"),
        description=a.get("description", ""),
        category=a.get("category", "General"),
        priority=a.get("priority", "🟡 Medium"),
        due_date=a.get("due_date", ""),
        status=a.get("status", "📋 To Do"),
    ))

@app.route("/api/tasks/<int:task_id>/status", methods=["POST"])
def api_update_task(task_id):
    if not GOOGLE_OK: return jsonify({"success": False, "error": "Google not configured"}), 503
    return jsonify(gs.update_task_status(task_id, (request.get_json() or {}).get("status", "")))

@app.route("/api/events")
def api_events():
    if not GOOGLE_OK: return jsonify({"success": False, "error": "Google not configured"}), 503
    return jsonify(gs.list_calendar_events(max_results=20))

@app.route("/api/chats")
def api_chats():
    if not GOOGLE_OK: return jsonify({"success": False, "chats": []}), 503
    try:
        return jsonify(gs.get_chat_history(max_rows=200))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/meetings")
def api_meetings():
    if not GOOGLE_OK: return jsonify({"success": False, "error": "Google not configured"}), 503
    return jsonify(gs.get_sheet_meetings(max_rows=50))

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  WhatsApp AI Manager — powered by Claude")
    print("=" * 55)
    print(f"  Verify Token : {VERIFY_TOKEN}")
    print(f"  Google       : {'✅' if GOOGLE_OK else '⚠️  Disabled'}")
    print(f"  Whisper STT  : {'✅' if WHISPER_OK else '⚠️  No OPENAI_API_KEY'}")
    print(f"  PDF reader   : {'✅' if PDF_OK else '⚠️  No pdfplumber'}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False)
