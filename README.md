# 🤖 WhatsApp AI Personal Assistant

> A complete personal/business AI assistant on WhatsApp — built on Claude (Haiku 4.5), with calendar booking, task management, document storage, voice transcription, and a luxury web dashboard.

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Features & Capabilities](#features--capabilities)
3. [Architecture](#architecture)
4. [Tech Stack](#tech-stack)
5. [Prerequisites](#prerequisites)
6. [Setup Guide (Step by Step)](#setup-guide-step-by-step)
7. [Local Development](#local-development)
8. [Deployment to Render](#deployment-to-render)
9. [Customizing for a Client](#customizing-for-a-client)
10. [File Structure](#file-structure)
11. [Function Reference](#function-reference)
12. [API Endpoints (Dashboard)](#api-endpoints-dashboard)
13. [Troubleshooting](#troubleshooting)

---

## Overview

This is a personal AI manager that lives on WhatsApp. Send it text, voice notes, images, or PDFs in any language and it acts like a smart human assistant:

- Books Google Meet calls with auto-generated links
- Manages your task list with due dates and priorities
- Saves PDFs to Google Drive
- Transcribes voice notes via OpenAI Whisper
- Sends daily 8 AM task summaries
- Tracks every conversation in a beautiful web dashboard

**Two-tier access:**
- **Owner number** (you) → full access to everything
- **All other numbers** (clients/visitors) → restricted to meeting booking + general chat

---

## Features & Capabilities

### 💬 Smart Conversation
- Replies in any language (English, Urdu, Roman Urdu, Hinglish, etc.) — auto-detects per message
- Persistent memory per phone number (Supabase-backed)
- Natural human-like tone — not robotic
- Smart message batching — waits 20 seconds for follow-ups, replies once

### 📅 Calendar Integration
- Books Google Calendar events with auto-generated Google Meet links
- Enforces business hours (9 AM – 5 PM Pakistan Time)
- Checks availability before every booking — suggests alternatives if busy
- Default duration buttons: 30 minutes / 1 hour
- All times in PKT (Asia/Karachi)
- Logs every meeting to a Google Sheet

### ✅ Task Management
- Add, list, update, and delete tasks via WhatsApp
- Priority levels: 🔴 High · 🟡 Medium · 🟢 Low
- Stages: 📋 To Do → 🔄 In Progress → 👀 Review → ✅ Done
- Filter: today / this week / this month / overdue
- Saves to BOTH Supabase (for dashboard) AND Google Sheets (for visibility)

### 📄 Document Handling (PDF / Images / Files)
- Sends 3 action buttons when owner uploads a PDF: **Save to Drive · Summarize · Key Actions**
- Saves files to Google Drive folder *"WhatsApp Documents"*
- Returns shareable Drive links
- Logs every uploaded document in a Google Sheet
- Reads first 15 pages (4000 chars max) for fast processing
- Vision support for images (describe, extract text, identify, etc.)

### 🎤 Voice Notes
- Transcribed via OpenAI Whisper
- Bot replies with the transcription + a contextual response
- Works in any language

### 🔘 WhatsApp Buttons
- Smart use of interactive buttons for quick choices
- Greeting menu, action menus, duration picker
- Falls back to plain text when buttons don't fit

### 📊 Web Dashboard
- Gold & White luxury theme with light/dark toggle
- Mobile-responsive (hamburger menu, touch-friendly)
- Live chat history viewer with search
- Task board with status filters
- Calendar event list
- Contact list (every WhatsApp user who has interacted)

### 🔔 Daily Reminders
- 8:00 AM PKT automatic summary sent to owner's WhatsApp
- Lists today's tasks + overdue items

### 🛡️ Access Control
- Owner phone number gets full tool access (tasks, drive, everything)
- All other numbers get only meeting booking + general conversation
- Polite refusal if a client asks about owner's private data

---

## Architecture

```
┌──────────────────┐                ┌─────────────┐
│  WhatsApp User   │                │  Dashboard  │
└────────┬─────────┘                └──────┬──────┘
         │ Webhook                         │ HTTP
         ▼                                 ▼
┌─────────────────────────────────────────────────┐
│         Flask Server (whatsapp_agent.py)        │
│  ┌─────────────────────────────────────────┐    │
│  │  Background thread per message          │    │
│  │  - Dedup (Meta retry guard)             │    │
│  │  - Batch (20s debounce for text)        │    │
│  │  - Type routing (text/image/pdf/audio)  │    │
│  └─────────────┬───────────────────────────┘    │
│                ▼                                 │
│  ┌─────────────────────────────────────────┐    │
│  │  ask_claude(phone, content)             │    │
│  │  - Loads conversation history           │    │
│  │  - Sanitizes corrupted history          │    │
│  │  - Calls Claude API with tools          │    │
│  │  - Executes tool calls in loop          │    │
│  │  - Saves history back to Supabase       │    │
│  └─────────────┬───────────────────────────┘    │
└────────────────┼─────────────────────────────────┘
                 │
        ┌────────┼────────┬────────────┬───────────┐
        ▼        ▼        ▼            ▼           ▼
   ┌────────┐ ┌──────┐ ┌──────────┐ ┌───────┐ ┌────────┐
   │ Claude │ │Sheets│ │ Calendar │ │ Drive │ │Supabase│
   │  API   │ │ API  │ │   API    │ │  API  │ │   DB   │
   └────────┘ └──────┘ └──────────┘ └───────┘ └────────┘
```

---

## Tech Stack

| Layer | Tool |
|---|---|
| AI | Anthropic Claude Haiku 4.5 |
| Voice | OpenAI Whisper |
| Backend | Python 3.11 + Flask |
| Database | Supabase (Postgres) |
| Calendar/Sheets/Drive | Google APIs (OAuth2 + Service Account) |
| Messaging | Meta WhatsApp Cloud API |
| Hosting | Render.com |
| Local tunnel | ngrok (for development) |

---

## Prerequisites

You'll need accounts on (all free tier OK to start):

1. [Anthropic Console](https://console.anthropic.com) — for Claude API key
2. [OpenAI](https://platform.openai.com) — *optional*, for voice transcription
3. [Supabase](https://supabase.com) — for chat history + tasks DB
4. [Google Cloud Console](https://console.cloud.google.com) — for Calendar/Sheets/Drive
5. [Meta for Developers](https://developers.facebook.com) — for WhatsApp Business API
6. [Render.com](https://render.com) — for production hosting
7. [GitHub](https://github.com) — to deploy via Render
8. [ngrok](https://ngrok.com) — for local webhook testing

---

## Setup Guide (Step by Step)

### Step 1 — Anthropic API Key
1. Go to https://console.anthropic.com → API Keys
2. Create a new key
3. Save it as `ANTHROPIC_API_KEY` in `.env`

### Step 2 — Supabase Setup
1. Create a project at https://supabase.com
2. Go to **SQL Editor** and run these table-creation queries:
   ```sql
   create table chat_history (
     id bigint generated by default as identity primary key,
     phone text not null,
     direction text,
     message text,
     msg_type text,
     created_at timestamptz default now()
   );

   create table conversations (
     phone text primary key,
     messages jsonb,
     updated_at timestamptz default now()
   );

   create table tasks (
     id bigint generated by default as identity primary key,
     task_name text not null,
     description text,
     category text,
     priority text,
     status text default '📋 To Do',
     due_date text,
     created_at timestamptz default now(),
     updated_at timestamptz default now()
   );
   ```
3. Settings → API → copy `URL` and `anon public` key
4. Add to `.env` as `SUPABASE_URL` and `SUPABASE_KEY`

### Step 3 — Google Cloud Setup

#### 3a. Create a project
1. https://console.cloud.google.com → Create new project
2. Note the project ID

#### 3b. Enable required APIs
Enable each:
- [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
- [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
- [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)

#### 3c. Create a Service Account (for Sheets access)
1. https://console.cloud.google.com/iam-admin/serviceaccounts → Create
2. Name it (e.g., `whatsapp-bot`)
3. Click into the new service account → **Keys** → **Add Key → Create new key → JSON**
4. Download → save as `service_account.json` in project folder
5. Copy the `client_email` from the JSON
6. Open your Google Sheet → Share → paste service account email → "Editor" access

#### 3d. Create OAuth 2.0 Client (for Calendar with Meet links)
1. Configure consent screen at https://console.cloud.google.com/apis/credentials/consent
   - User type: External → Create
   - Add yourself as **Test User**
   - Save
2. Create credentials at https://console.cloud.google.com/apis/credentials
   - **+ Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download JSON → save as `oauth_client.json`
3. Run the auth script ONCE:
   ```cmd
   python get_token.py
   ```
   - Browser opens → sign in → click **Advanced → Go to (unsafe)** → **Allow**
   - Generates `token.json`

#### 3e. Get your Calendar ID
1. https://calendar.google.com → click your calendar → **Settings and sharing**
2. Scroll to **Integrate calendar** → copy **Calendar ID** (your Gmail or `xxx@group.calendar.google.com`)
3. Add to `.env` as `GOOGLE_CALENDAR_ID`

#### 3f. Get your Spreadsheet ID
1. Create or open a Google Sheet
2. URL: `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
3. Copy the ID and add to `.env` as `GOOGLE_SPREADSHEET_ID`

### Step 4 — Meta WhatsApp Business API
1. https://developers.facebook.com → My Apps → Create App
2. Type: **Business**
3. Add product: **WhatsApp**
4. Get a temporary access token + Phone Number ID from the WhatsApp settings
5. Add to `.env`:
   ```
   WHATSAPP_TOKEN=...
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_VERIFY_TOKEN=any_string_you_choose
   ```

### Step 5 — OpenAI (Optional, for voice)
1. https://platform.openai.com/api-keys → create key
2. Add to `.env` as `OPENAI_API_KEY`

### Step 6 — Configure `.env` file
Use `.env.example` as a template. Final file:

```bash
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Google
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_CALENDAR_ID=your.email@gmail.com
GOOGLE_SPREADSHEET_ID=1ABCdef...
GOOGLE_SHEET_NAME=Meetings

# WhatsApp
WHATSAPP_TOKEN=EAAxxxxx
WHATSAPP_PHONE_NUMBER_ID=123456789
WHATSAPP_VERIFY_TOKEN=mysecrettoken

# OpenAI
OPENAI_API_KEY=sk-...

# Supabase
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=eyJxxx...

# Daily reminder (your WhatsApp number with country code, no +)
REMINDER_PHONE=923191413828
```

---

## Local Development

### Install dependencies
```cmd
pip install -r requirements.txt
```

### Run the bot
```cmd
python whatsapp_agent.py
```

### Expose to internet via ngrok
```cmd
ngrok http 5000
```

Copy the `https://...ngrok.io` URL.

### Connect to Meta
- Meta dashboard → WhatsApp → Configuration
- Callback URL: `https://your-ngrok-url.ngrok.io/webhook`
- Verify Token: same value as `WHATSAPP_VERIFY_TOKEN` in `.env`
- Click **Verify and Save**
- Subscribe to webhook field: `messages`

### Test
- Send a WhatsApp message to your test number
- Bot should reply
- Visit http://localhost:5000 to see the dashboard

---

## Deployment to Render

### Step 1 — Push to GitHub
```cmd
git add .
git commit -m "Initial commit"
git push origin main
```

⚠️ Make sure `.gitignore` excludes:
```
.env
service_account.json
oauth_client.json
token.json
__pycache__/
```

### Step 2 — Create Render Web Service
1. https://render.com → New → Web Service
2. Connect your GitHub repo
3. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn whatsapp_agent:app --workers 2 --threads 4 --timeout 120`

### Step 3 — Add Environment Variables on Render
Add ALL from your `.env` file PLUS these two:

| Key | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire content of `service_account.json` (one line) |
| `GOOGLE_OAUTH_TOKEN_JSON` | Entire content of `token.json` (one line) |

⚠️ **Don't include `GOOGLE_SERVICE_ACCOUNT_FILE`** on Render — Render has no filesystem.

### Step 4 — Update Meta Webhook
- Meta dashboard → WhatsApp → Webhook
- Change callback URL to: `https://your-render-url.onrender.com/webhook`
- Same verify token

---

## Customizing for a Client

When delivering this bot to a new client, change these things:

### 🔧 1. Owner Phone Number
**File:** `whatsapp_agent.py` line ~31
```python
OWNER_PHONES = {"923191413828", "3191413828", "03191413828"}  # all formats
```
Replace with the client's WhatsApp number in all 3 formats (with/without country code, with/without leading 0).

### 🔧 2. Owner Name & Branding
**File:** `whatsapp_agent.py` — search and replace these strings:

| Find | Replace With |
|---|---|
| `Muhammad Daud Zia` | Client's full name |
| `Daud` | Client's first name |

Locations: system prompts (OWNER_SYSTEM_PROMPT, CLIENT_SYSTEM_PROMPT), startup banner.

**File:** `dashboard.html` — same name replacements (search for "Daud").

### 🔧 3. Business Hours
**File:** `whatsapp_agent.py` — search for **"BUSINESS HOURS RULE"** and adjust:
- Default: **9 AM – 5 PM Pakistan Time**
- Update both OWNER and CLIENT system prompts

### 🔧 4. Timezone
**File:** `google_services.py` — search for `Asia/Karachi`:
```python
"start": {"dateTime": start_iso, "timeZone": "Asia/Karachi"},
```
Change to client's IANA timezone (e.g., `Europe/London`, `America/New_York`).

**Also:** `whatsapp_agent.py` — scheduler uses `Asia/Karachi`:
```python
sched = BackgroundScheduler(timezone=timezone("Asia/Karachi"))
```

### 🔧 5. Daily Reminder Time
**File:** `whatsapp_agent.py` — find:
```python
sched.add_job(send_daily_summary, "cron", hour=8, minute=0)
```
Change `hour=8` to whatever time the client wants the summary.

### 🔧 6. Greeting Buttons
**File:** `whatsapp_agent.py` — system prompts have button labels like:
```
"📋 Tasks", "📅 Calendar", "➕ Add Task"
```
Customize for client's specific use case.

### 🔧 7. New Google + Supabase Accounts
The client needs THEIR own:
- Google Cloud project (their calendar, sheets, drive)
- Supabase project
- Anthropic API key (or use yours and bill them)
- Meta WhatsApp Business account (verified)
- Render account

Repeat the **Setup Guide** with their credentials.

### 🔧 8. Permanent Meta Token
For production:
- Client must complete Meta business verification
- Generate a permanent system user access token (not the 24-hour test one)

---

## File Structure

```
E:\test\
├── whatsapp_agent.py        ← Main Flask server + Claude agent
├── google_services.py       ← Calendar, Sheets, Drive functions
├── supabase_services.py     ← Database (chat history, conversations, tasks)
├── get_token.py             ← One-time OAuth setup script
├── dashboard.html           ← Web dashboard UI
├── dashboard.css            ← Dashboard styles
├── requirements.txt         ← Python dependencies
├── .env                     ← Secrets (DO NOT COMMIT)
├── .env.example             ← Template for new deployments
├── .gitignore               ← Excludes secrets from git
├── service_account.json     ← Google service account creds (DO NOT COMMIT)
├── oauth_client.json        ← OAuth client config (DO NOT COMMIT)
├── token.json               ← OAuth refresh token (DO NOT COMMIT)
├── render.yaml              ← Render deployment config
├── Procfile                 ← Render process declaration
└── README.md                ← This file
```

---

## Function Reference

### Core (`whatsapp_agent.py`)

| Function | Purpose |
|---|---|
| `ask_claude(phone, content)` | Main agent loop — calls Claude with history, handles tool use |
| `handle_text/image/document/audio()` | Per-type message handlers |
| `queue_or_process()` | Routes to batching (text) or immediate (media) |
| `_flush_batch(phone)` | Combines buffered text messages and replies once |
| `is_owner(phone)` | Checks if sender has full access |
| `get_system_prompt(phone)` | Returns owner or client prompt |
| `wa_format(text)` | Converts markdown to WhatsApp format (`**bold**` → `*bold*`) |
| `_serialize_content()` | Converts Anthropic SDK objects to JSON-safe dicts |
| `mark_read_and_typing()` | Shows read receipt + typing indicator |
| `send_text/buttons()` | Sends WhatsApp messages |
| `is_duplicate(msg_id)` | Prevents Meta retry double-processing |
| `send_daily_summary()` | 8 AM cron job for task reminder |

### Tools (Claude can call these)

| Tool | What it does |
|---|---|
| `add_task` | Adds task to Supabase + Sheets |
| `get_tasks` | Lists tasks with filters (status, period, search) |
| `update_task_status` | Changes task stage |
| `create_calendar_event` | Books meeting with Google Meet link |
| `list_calendar_events` | Lists upcoming meetings (used for availability check) |
| `save_document_to_drive` | Uploads PDF to Drive + logs to Sheets |
| `send_button_menu` | Sends interactive WhatsApp button message |

### Google Services (`google_services.py`)

| Function | Purpose |
|---|---|
| `_get_oauth_credentials()` | Loads user OAuth token (preferred — supports Meet) |
| `_get_credentials()` | Loads service account creds (fallback) |
| `ensure_all_sheets()` | Creates Tasks/Meetings/Documents/Chats tabs at startup |
| `add_task / get_tasks / update_task_status / delete_task` | CRUD on Tasks sheet |
| `create_calendar_event()` | Creates event with auto Meet link |
| `list_calendar_events()` | Lists upcoming events |
| `_parse_dt()` | Robust datetime parser (handles "tomorrow 3pm", ISO, etc.) |
| `upload_to_drive()` | Saves file to Drive folder + makes shareable |
| `append_meeting_to_sheet()` | Logs booked meeting |
| `append_document_to_sheet()` | Logs uploaded document |

### Supabase (`supabase_services.py`)

| Function | Purpose |
|---|---|
| `save_message / get_chat_history` | Per-message log for dashboard |
| `save_conversation / load_conversation` | Claude memory per phone |
| `add_task / get_tasks / update_task_status / delete_task` | Task CRUD |
| `get_contacts / delete_chat / get_stats` | Dashboard support |

---

## API Endpoints (Dashboard)

All return JSON.

| Endpoint | Method | Returns |
|---|---|---|
| `/` | GET | Dashboard HTML |
| `/api/stats` | GET | System stats (Google/Supabase/Whisper status, event count) |
| `/api/tasks?status=&search=` | GET | Filtered task list |
| `/api/tasks/add` | POST | Add a new task |
| `/api/tasks/<id>/status` | POST | Update task stage |
| `/api/tasks/<id>` | DELETE | Delete a task |
| `/api/chats?phone=` | GET | Chat history (filtered by phone) |
| `/api/contacts` | GET | All contacts who messaged the bot |
| `/api/chats/<phone>` | DELETE | Delete chat + reset memory |
| `/api/memory/<phone>/reset` | POST | Reset Claude's memory for a phone |
| `/api/events` | GET | Upcoming calendar events |
| `/api/meetings` | GET | Logged meetings from Sheets |
| `/webhook` | GET/POST | Meta WhatsApp webhook |

---

## Troubleshooting

### "Calendar is currently having a technical issue"
- The bot is hiding the real error. Check terminal logs for the line starting `⚠️ First attempt failed:`
- Most common: OAuth token revoked → re-run `python get_token.py`

### "Object of type TextBlock is not JSON serializable"
- Old corrupted history. Reset for that phone:
  ```cmd
  curl -X POST http://localhost:5000/api/memory/PHONE/reset
  ```

### Sheets not updating
- API not enabled — check the URL in error message and click ENABLE
- Service account email not shared with the sheet → add as Editor
- Wrong `GOOGLE_SPREADSHEET_ID` in `.env` (must be just the ID, not full URL)

### No Google Meet link generated
- Service account can't create Meet links — needs OAuth
- Run `python get_token.py` and ensure `token.json` exists locally
- On Render, make sure `GOOGLE_OAUTH_TOKEN_JSON` env var is set

### Webhook not verifying on Meta
- Make sure `WHATSAPP_VERIFY_TOKEN` in `.env` matches what you typed in Meta
- Server must be running (locally OR on Render)
- ngrok URL changes every time you restart ngrok

### Multiple replies to same message
- Meta retries when responses take >10s
- Bot already deduplicates by message ID, but check terminal for `🔁 Duplicate` line

### Render deployment crashes
- Check Render logs for the actual error
- Most common: missing env var, or `GOOGLE_SERVICE_ACCOUNT_FILE` instead of `_JSON`

---

## License

MIT — adapt freely for your projects.

---

## Credits

Built by Muhammad Daud Zia.  
Powered by Claude (Anthropic), OpenAI Whisper, Google Workspace APIs, Supabase, Meta WhatsApp Cloud API.
