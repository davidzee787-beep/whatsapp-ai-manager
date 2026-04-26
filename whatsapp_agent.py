"""
Personal WhatsApp AI Manager — powered by Claude + Supabase
Supports: Text, Images, Documents, Voice Notes, Any Language
Memory: Supabase (persistent conversation history)
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
        caption = msg["image"].get("caption","")
        parts = [
            {"type":"image","source":{"type":"base64","media_type":mime,"data":base64.b64encode(content).decode()}},
            {"type":"text","text": caption or "What's in this image? Be detailed and helpful."},
        ]
        return ask_claude(phone, parts)
    except Exception as e:
        return f"❌ Couldn't read image: {e}"

def handle_document(phone: str, msg: dict) -> str:
    doc      = msg["document"]
    filename = doc.get("filename","document")
    mime     = doc.get("mime_type","")
    try:
        content, mime_type = download_wa_media(doc["id"])
        if "pdf" in mime.lower() or filename.lower().endswith(".pdf"):
            if not PDF_OK:
                return "📄 PDF support not installed. Run: pip install pdfplumber"
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n\n".join(
                    f"[Page {i+1}]\n{(p.extract_text() or '').strip()}"
                    for i, p in enumerate(pdf.pages) if p.extract_text()
                )
            if not text.strip():
                return f"📄 *{filename}* — couldn't extract text (may be a scanned PDF)."
            parts = [{"type":"text","text":f"Document: *{filename}*\n\n{text[:6000]}\n\nSummarise and help with this."}]
            return ask_claude(phone, parts)
        elif "image" in mime.lower():
            if mime_type not in ("image/jpeg","image/png","image/gif","image/webp"):
                mime_type = "image/jpeg"
            parts = [
                {"type":"image","source":{"type":"base64","media_type":mime_type,"data":base64.b64encode(content).decode()}},
                {"type":"text","text":f"Document image: '{filename}'. What does it say or show?"},
            ]
            return ask_claude(phone, parts)
        elif "text" in mime.lower():
            text  = content.decode("utf-8", errors="ignore")[:5000]
            parts = [{"type":"text","text":f"File: *{filename}*\n\n{text}\n\nAnalyse this."}]
            return ask_claude(phone, parts)
        else:
            return f"📎 Received *{filename}* but can't read this file type yet.\n\nI support: PDFs, Images, Text files."
    except Exception as e:
        return f"❌ Couldn't read document: {e}"

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
        "description": "List tasks. Can filter by status or search by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter":{"type":"string","default":""},
                "search":       {"type":"string","default":""},
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
        "description": "Schedule a meeting or event on Google Calendar.",
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
        "description": "Show upcoming schedule.",
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
                    return json.dumps(svc.get_tasks(status_filter=inp.get("status_filter",""), search=inp.get("search","")))
                else:
                    return json.dumps(svc.get_tasks(status_filter=inp.get("status_filter","")))
            elif name == "update_task_status":
                return json.dumps(svc.update_task_status(inp["task_id"], inp["new_status"]))
        except Exception as e:
            return json.dumps({"error": str(e)})

    # Calendar — Google only
    if not GOOGLE_OK:
        return json.dumps({"error": "Google Calendar not configured"})
    try:
        if name == "create_calendar_event":
            result = gs.create_calendar_event(
                title=inp["title"], start_datetime=inp["start_datetime"],
                end_datetime=inp["end_datetime"], description=inp.get("description",""),
                attendees=inp.get("attendees") or [], location=inp.get("location",""),
            )
            if result.get("success"):
                gs.append_meeting_to_sheet(
                    title=result["title"], start_datetime=result["start"],
                    end_datetime=result["end"], attendees=", ".join(inp.get("attendees") or []),
                    description=inp.get("description",""), event_link=result.get("event_link",""),
                )
            return json.dumps(result)
        elif name == "list_calendar_events":
            return json.dumps(gs.list_calendar_events(max_results=inp.get("max_results",10)))
    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"error": f"Unknown tool: {name}"})

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are a personal AI manager on WhatsApp. Today: {datetime.utcnow().strftime('%Y-%m-%d')}.

🌍 LANGUAGE: Always reply in the SAME language the user writes in. Auto-detect every message.

You handle:
- 💬 Text — chat, tasks, schedule
- 🖼️ Images — describe and analyse
- 📄 Documents/PDFs — read and summarise
- 🎤 Voice notes — respond to transcription
- 🔘 Buttons — send quick action menus

Task stages: 📋 To Do → 🔄 In Progress → 👀 Review → ✅ Done → ❌ Cancelled

Style: SHORT replies (max 6 lines), *bold*, _italic_, emojis. After actions, send a button menu."""

# ---------------------------------------------------------------------------
# Claude agent loop
# ---------------------------------------------------------------------------
def ask_claude(phone: str, content: list) -> str:
    history = load_history(phone)
    history.append({"role": "user", "content": content})

    while True:
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history[-20:],
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
# WhatsApp senders
# ---------------------------------------------------------------------------
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
        from_num = msg["from"]
        msg_type = msg.get("type","")
        print(f"\n📩 [{msg_type}] from {from_num}")

        if msg_type == "text":
            ut = msg["text"]["body"]
            log_chat(from_num,"IN",ut,"text")
            reply = handle_text(from_num, ut)
        elif msg_type == "image":
            log_chat(from_num,"IN",f"[Image] {msg['image'].get('caption','')}","image")
            send_text(from_num,"🖼️ Reading your image...")
            reply = handle_image(from_num, msg)
        elif msg_type == "document":
            log_chat(from_num,"IN",f"[Doc] {msg['document'].get('filename','')}","document")
            send_text(from_num,f"📄 Reading *{msg['document'].get('filename','document')}*...")
            reply = handle_document(from_num, msg)
        elif msg_type == "audio":
            log_chat(from_num,"IN","[Voice note]","audio")
            send_text(from_num,"🎤 Transcribing...")
            reply = handle_audio(from_num, msg)
        elif msg_type == "interactive":
            iv = msg.get("interactive",{})
            if iv.get("type") == "button_reply":
                bt = iv["button_reply"]["title"]
                log_chat(from_num,"IN",bt,"button")
                reply = handle_text(from_num, bt)
            else: return jsonify({"status":"ok"}),200
        elif msg_type == "sticker":
            reply = ask_claude(from_num,[{"type":"text","text":"The user sent a sticker. Reply playfully!"}])
        else:
            reply = f"I received your {msg_type} — I'll support this soon! 😊"

        if reply:
            send_text(from_num, reply)
            log_chat(from_num,"OUT",reply,"text")
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
    return jsonify(sb.delete_chat(phone))

@app.route("/api/events")
def api_events():
    if not GOOGLE_OK: return jsonify({"success":False,"error":"Google not configured"}),503
    return jsonify(gs.list_calendar_events(max_results=20))

@app.route("/api/meetings")
def api_meetings():
    if not GOOGLE_OK: return jsonify({"success":False,"error":"Google not configured"}),503
    return jsonify(gs.get_sheet_meetings(max_rows=50))

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print("="*55)
    print("  WhatsApp AI Manager — Claude + Supabase")
    print("="*55)
    print(f"  Supabase : {'✅' if SUPABASE_OK else '⚠️  Disabled'}")
    print(f"  Google   : {'✅' if GOOGLE_OK else '⚠️  Disabled'}")
    print(f"  Whisper  : {'✅' if WHISPER_OK else '⚠️  No OPENAI_API_KEY'}")
    print("="*55)
    app.run(host="0.0.0.0", port=port, debug=False)
