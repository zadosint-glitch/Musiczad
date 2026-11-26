import os, sqlite3, time, secrets, hashlib
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash, Response

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(APP_DIR, "mafia_music.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEO_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_EMBED_BASE = "https://www.youtube.com/embed/"

def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS youtube_keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
            api_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            cooldown_until INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS api_keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
            api_key TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT, request_limit INTEGER, request_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            endpoint TEXT, query TEXT, status TEXT, detail TEXT)""")
        # Optional bootstrap key from environment; never expose it in the dashboard.
        env_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if env_key:
            exists = con.execute("SELECT id FROM youtube_keys WHERE api_key=?", (env_key,)).fetchone()
            if not exists:
                con.execute("INSERT INTO youtube_keys(label,api_key,created_at) VALUES(?,?,?)",
                            ("Environment key", env_key, now()))
        # First API key is generated once and printed to server logs.
        if not con.execute("SELECT id FROM api_keys").fetchone():
            initial = "maf_" + secrets.token_urlsafe(24)
            con.execute("INSERT INTO api_keys(label,api_key,created_at) VALUES(?,?,?)",
                        ("Default API key", initial, now()))
            print("\\n[MAFIA MUSIC] Initial client API key (save it now):", initial, "\\n")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def log(endpoint, query, status, detail=""):
    with db() as con:
        con.execute("INSERT INTO logs(created_at,endpoint,query,status,detail) VALUES(?,?,?,?,?)",
                    (now(), endpoint, (query or "")[:180], status, (detail or "")[:400]))

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_ok"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def client_key_valid(key):
    if not key:
        return False, "Missing api_key"
    with db() as con:
        row = con.execute("SELECT * FROM api_keys WHERE api_key=? AND enabled=1", (key,)).fetchone()
        if not row:
            return False, "Invalid or disabled API key"
        if row["expires_at"]:
            try:
                expiry = datetime.fromisoformat(row["expires_at"].replace("Z","+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    return False, "API key expired"
            except ValueError:
                return False, "Invalid API key expiry configuration"
        if row["request_limit"] is not None and row["request_count"] >= row["request_limit"]:
            return False, "API key request limit reached"
        con.execute("UPDATE api_keys SET request_count=request_count+1 WHERE id=?", (row["id"],))
    return True, ""

def yt_keys():
    with db() as con:
        return con.execute("""SELECT * FROM youtube_keys WHERE enabled=1
            AND cooldown_until<=? ORDER BY id ASC""", (int(time.time()),)).fetchall()

def is_quota_or_key_error(payload, status):
    reason = ""
    try:
        reason = payload.get("error",{}).get("errors",[{}])[0].get("reason","")
    except Exception:
        pass
    # Rotate away from quota/invalid/forbidden keys; transient errors also try next key.
    return status in (403, 429, 500, 502, 503) or reason in (
        "quotaExceeded", "dailyLimitExceeded", "keyInvalid", "forbidden",
        "rateLimitExceeded", "userRateLimitExceeded")

def youtube_request(params):
    keys = yt_keys()
    if not keys:
        return None, (503, "No active YouTube API keys. Add keys in Admin > YouTube Keys.")
    last_message = "YouTube request failed"
    for row in keys:
        p = dict(params)
        p["key"] = row["api_key"]
        try:
            r = requests.get(YOUTUBE_SEARCH_URL, params=p, timeout=15)
            try:
                payload = r.json()
            except Exception:
                payload = {"error":{"message":r.text[:200]}}
            if r.ok and "error" not in payload:
                return payload, None
            message = payload.get("error",{}).get("message", f"HTTP {r.status_code}")
            last_message = message
            if is_quota_or_key_error(payload, r.status_code):
                with db() as con:
                    con.execute("UPDATE youtube_keys SET cooldown_until=?,last_error=? WHERE id=?",
                                (int(time.time()) + 3600, message[:300], row["id"]))
                continue
            return None, (502, message)
        except requests.RequestException as e:
            last_message = str(e)
            continue
    return None, (503, "All active YouTube API keys failed or are cooling down: " + last_message)

def video_info(video_id):
    keys = yt_keys()
    for row in keys:
        try:
            r = requests.get(YOUTUBE_VIDEO_URL, params={
                "part":"snippet,contentDetails,statistics", "id":video_id, "key":row["api_key"]
            }, timeout=12)
            payload = r.json()
            if r.ok and payload.get("items"):
                item = payload["items"][0]
                sn = item.get("snippet", {})
                thumbs = sn.get("thumbnails", {})
                thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
                return {
                    "videoId": video_id, "title": sn.get("title"),
                    "channelTitle": sn.get("channelTitle"), "publishedAt": sn.get("publishedAt"),
                    "description": sn.get("description"), "thumbnail": thumb,
                    "duration": item.get("contentDetails",{}).get("duration"),
                    "viewCount": item.get("statistics",{}).get("viewCount")
                }
            message = payload.get("error",{}).get("message","Video details unavailable")
            if is_quota_or_key_error(payload, r.status_code):
                with db() as con:
                    con.execute("UPDATE youtube_keys SET cooldown_until=?,last_error=? WHERE id=?",
                                (int(time.time()) + 3600, message[:300], row["id"]))
                continue
        except Exception:
            continue
    return None

@app.get("/")
def home():
    return render_template("home.html")

@app.get("/api/health")
def health():
    with db() as con:
        count = con.execute("SELECT COUNT(*) FROM youtube_keys WHERE enabled=1").fetchone()[0]
    return jsonify({"ok": True, "service":"MAFIA MUSIC API PRO", "active_youtube_keys":count,
                    "modes":["info","play","both"]})

@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    mode = (request.args.get("type") or "info").lower()
    client_key = request.args.get("api_key") or request.headers.get("X-API-Key")
    if not q:
        return jsonify({"ok":False,"error":"Missing q parameter"}), 400
    if mode not in ("info","play","both"):
        return jsonify({"ok":False,"error":"type must be info, play, or both"}), 400
    valid, err = client_key_valid(client_key)
    if not valid:
        log("/api/search", q, "denied", err)
        return jsonify({"ok":False,"error":err}), 401
    payload, error = youtube_request({
        "part":"snippet", "q":q, "type":"video", "maxResults":5,
        "safeSearch":"strict"
    })
    if error:
        code, message = error
        log("/api/search", q, "error", message)
        return jsonify({"ok":False,"error":message}), code
    results = []
    for item in payload.get("items",[]):
        vid = item.get("id",{}).get("videoId")
        sn = item.get("snippet",{})
        if not vid:
            continue
        thumbnails = sn.get("thumbnails",{})
        thumbnail = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
        base = {"videoId":vid,"title":sn.get("title"),"channelTitle":sn.get("channelTitle"),
                "publishedAt":sn.get("publishedAt"),"description":sn.get("description"),
                "thumbnail":thumbnail,"youtubeUrl":"https://www.youtube.com/watch?v="+vid}
        if mode in ("play","both"):
            base["player"] = {"provider":"YouTube","embedUrl":YOUTUBE_EMBED_BASE+vid,
                              "watchUrl":base["youtubeUrl"],"autoplay":False}
        results.append(base)
    log("/api/search", q, "success", f"{len(results)} results; type={mode}")
    if mode == "play":
        return jsonify({"ok":True,"type":"play","results":results})
    if mode == "both":
        return jsonify({"ok":True,"type":"both","results":results})
    return jsonify({"ok":True,"type":"info","results":results})

@app.get("/player/<video_id>")
def player(video_id):
    if not (len(video_id) == 11 and all(c.isalnum() or c in "-_" for c in video_id)):
        return "Invalid YouTube video ID", 400
    return render_template("player.html", video_id=video_id)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        user = os.environ.get("ADMIN_USER", "MAFIA")
        password = os.environ.get("ADMIN_PASSWORD", "CHANGE_THIS_PASSWORD")
        if request.form.get("username") == user and secrets.compare_digest(
            request.form.get("password",""), password):
            session["admin_ok"] = True
            return redirect(url_for("admin"))
        flash("Login details incorrect.")
    return render_template("login.html")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.get("/admin")
@admin_required
def admin():
    with db() as con:
        yt = con.execute("SELECT id,label,enabled,cooldown_until,last_error,created_at FROM youtube_keys ORDER BY id DESC").fetchall()
        clients = con.execute("SELECT id,label,enabled,expires_at,request_limit,request_count,created_at FROM api_keys ORDER BY id DESC").fetchall()
        logs = con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 60").fetchall()
    return render_template("admin.html", yt=yt, clients=clients, logs=logs, now_ts=int(time.time()))

@app.post("/admin/youtube/add")
@admin_required
def add_youtube():
    label = (request.form.get("label") or "YouTube key").strip()[:80]
    key = (request.form.get("api_key") or "").strip()
    if not key:
        flash("YouTube API key required.")
    else:
        with db() as con:
            try:
                con.execute("INSERT INTO youtube_keys(label,api_key,created_at) VALUES(?,?,?)",(label,key,now()))
                flash("YouTube key added.")
            except sqlite3.IntegrityError:
                flash("This YouTube key is already added.")
    return redirect(url_for("admin"))

@app.post("/admin/youtube/<int:key_id>/<action>")
@admin_required
def youtube_action(key_id, action):
    with db() as con:
        if action == "toggle":
            con.execute("UPDATE youtube_keys SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(key_id,))
        elif action == "reset":
            con.execute("UPDATE youtube_keys SET cooldown_until=0,last_error=NULL WHERE id=?",(key_id,))
        elif action == "delete":
            con.execute("DELETE FROM youtube_keys WHERE id=?",(key_id,))
    flash("YouTube key updated.")
    return redirect(url_for("admin"))

@app.post("/admin/client/add")
@admin_required
def add_client():
    label = (request.form.get("label") or "Client key").strip()[:80]
    expiry = (request.form.get("expires_at") or "").strip() or None
    limit_raw = (request.form.get("request_limit") or "").strip()
    try:
        limit = int(limit_raw) if limit_raw else None
        if limit is not None and limit < 1: raise ValueError()
        key = "maf_" + secrets.token_urlsafe(24)
        with db() as con:
            con.execute("INSERT INTO api_keys(label,api_key,expires_at,request_limit,created_at) VALUES(?,?,?,?,?)",
                        (label,key,expiry,limit,now()))
        flash("New client key: " + key)
    except ValueError:
        flash("Request limit must be a positive number.")
    return redirect(url_for("admin"))

@app.post("/admin/client/<int:key_id>/<action>")
@admin_required
def client_action(key_id, action):
    with db() as con:
        if action == "toggle":
            con.execute("UPDATE api_keys SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(key_id,))
        elif action == "delete":
            con.execute("DELETE FROM api_keys WHERE id=?",(key_id,))
        elif action == "reset":
            con.execute("UPDATE api_keys SET request_count=0 WHERE id=?",(key_id,))
    flash("Client API key updated.")
    return redirect(url_for("admin"))

@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"ok":False,"error":"Endpoint not found"}),404
    return "Not found",404

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
