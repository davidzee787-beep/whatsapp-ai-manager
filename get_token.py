"""
One-time OAuth setup — run this ONCE to authorize the bot to use your Google Calendar.

Steps:
  1. Make sure oauth_client.json is in this folder
  2. Run: python get_token.py
  3. Browser opens — sign in with your Gmail and click "Allow"
  4. token.json is saved — that's it. The bot now creates real Meet links forever.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CLIENT_FILE = os.path.join(BASE_DIR, "oauth_client.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "token.json")

def main():
    if not os.path.exists(CLIENT_FILE):
        print(f"❌ {CLIENT_FILE} not found.")
        print("   Download OAuth client JSON from Google Cloud Console")
        print("   and save it as oauth_client.json in this folder.")
        return

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.valid:
            print(f"✅ token.json already valid — bot is authorized.")
            print(f"   Account: {creds.client_id[:30]}...")
            return
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
                print(f"✅ Refreshed token. All good.")
                return
            except Exception as e:
                print(f"⚠️ Refresh failed: {e} — running fresh auth.")

    flow  = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print()
    print("✅" * 30)
    print(f"✅ Authorized! Saved to {TOKEN_FILE}")
    print(f"✅ Bot can now create Google Meet links automatically.")
    print(f"✅ Restart your bot:  python whatsapp_agent.py")
    print("✅" * 30)

if __name__ == "__main__":
    main()
