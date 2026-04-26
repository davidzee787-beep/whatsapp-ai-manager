"""
Supabase service layer — chat history, tasks, conversation memory.
"""
import os
from datetime import datetime
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_client: Client | None = None

def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client

# ---------------------------------------------------------------------------
# Chat History
# ---------------------------------------------------------------------------
def save_message(phone: str, direction: str, message: str, msg_type: str = "text") -> dict:
    try:
        res = get_client().table("chat_history").insert({
            "phone":     phone,
            "direction": direction.upper(),
            "message":   message[:2000],
            "msg_type":  msg_type,
        }).execute()
        return {"success": True, "data": res.data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_chat_history(phone: str = "", limit: int = 200) -> dict:
    try:
        q = get_client().table("chat_history").select("*").order("created_at", desc=True).limit(limit)
        if phone:
            q = q.eq("phone", phone)
        res = q.execute()
        return {"success": True, "chats": res.data or []}
    except Exception as e:
        return {"success": False, "chats": [], "error": str(e)}


def delete_chat(phone: str) -> dict:
    try:
        get_client().table("chat_history").delete().eq("phone", phone).execute()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_contacts() -> dict:
    """Get unique contacts with their last message."""
    try:
        res = get_client().table("chat_history").select("phone, message, direction, created_at").order("created_at", desc=True).execute()
        seen   = set()
        contacts = []
        for row in (res.data or []):
            if row["phone"] not in seen:
                seen.add(row["phone"])
                contacts.append(row)
        return {"success": True, "contacts": contacts}
    except Exception as e:
        return {"success": False, "contacts": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Conversation Memory (Claude context per user)
# ---------------------------------------------------------------------------
def load_conversation(phone: str) -> list:
    try:
        res = get_client().table("conversations").select("messages").eq("phone", phone).execute()
        if res.data:
            return res.data[0].get("messages", [])
        return []
    except Exception:
        return []


def save_conversation(phone: str, messages: list) -> None:
    try:
        # Keep last 20 messages to avoid bloat
        messages = messages[-20:]
        existing = get_client().table("conversations").select("id").eq("phone", phone).execute()
        if existing.data:
            get_client().table("conversations").update({
                "messages":   messages,
                "updated_at": datetime.utcnow().isoformat(),
            }).eq("phone", phone).execute()
        else:
            get_client().table("conversations").insert({
                "phone":      phone,
                "messages":   messages,
                "updated_at": datetime.utcnow().isoformat(),
            }).execute()
    except Exception as e:
        print(f"⚠️ Save conversation error: {e}")


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def add_task(task_name: str, description: str = "", category: str = "General",
             priority: str = "🟡 Medium", due_date: str = "", status: str = "📋 To Do") -> dict:
    try:
        res = get_client().table("tasks").insert({
            "task_name":   task_name,
            "description": description,
            "category":    category,
            "priority":    priority,
            "due_date":    due_date,
            "status":      status,
        }).execute()
        data = res.data[0] if res.data else {}
        return {"success": True, "task_id": data.get("id"), "task_name": task_name, "status": status}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_tasks(status_filter: str = "", search: str = "", limit: int = 100) -> dict:
    try:
        q = get_client().table("tasks").select("*").order("created_at", desc=True).limit(limit)
        if status_filter:
            q = q.ilike("status", f"%{status_filter}%")
        if search:
            q = q.ilike("task_name", f"%{search}%")
        res = q.execute()
        return {"success": True, "tasks": res.data or []}
    except Exception as e:
        return {"success": False, "tasks": [], "error": str(e)}


def update_task_status(task_id: int, new_status: str) -> dict:
    STATUSES = ["📋 To Do", "🔄 In Progress", "👀 Review", "✅ Done", "❌ Cancelled"]
    matched  = next((s for s in STATUSES if new_status.lower() in s.lower()), None)
    if not matched:
        return {"success": False, "error": f"Invalid status: {new_status}"}
    try:
        get_client().table("tasks").update({
            "status":     matched,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", task_id).execute()
        return {"success": True, "task_id": task_id, "new_status": matched}
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_task(task_id: int) -> dict:
    try:
        get_client().table("tasks").delete().eq("id", task_id).execute()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_stats() -> dict:
    try:
        tasks_res = get_client().table("tasks").select("status").execute()
        tasks     = tasks_res.data or []
        chats_res = get_client().table("chat_history").select("phone").execute()
        chats     = chats_res.data or []
        phones    = set(c["phone"] for c in chats)
        return {
            "success":          True,
            "total_tasks":      len(tasks),
            "todo_tasks":       sum(1 for t in tasks if "To Do"      in t.get("status", "")),
            "inprogress_tasks": sum(1 for t in tasks if "In Progress" in t.get("status", "")),
            "done_tasks":       sum(1 for t in tasks if "Done"        in t.get("status", "")),
            "total_messages":   len(chats),
            "active_contacts":  len(phones),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
