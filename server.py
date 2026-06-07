# -*- coding: utf-8 -*-
"""Kasandra Technologies - members site backend (Postgres-ready).
SAFETY: completely separate from the trading system. Reads results one-way only.

Storage: uses PostgreSQL when DATABASE_URL is set (Railway), otherwise falls back
to a local JSON file (members.json) for local testing. Same API either way.
"""
from fastapi import FastAPI, Request, Cookie
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
import json, os, hashlib, secrets, time, re

app = FastAPI()
SITE = os.path.dirname(os.path.abspath(__file__))
MEMBERS_FILE = os.path.join(SITE, "members.json")
RESULTS_FILE = os.path.join(SITE, "public_results.json")
SIGNALS_FILE = os.path.join(SITE, "live_signals.json")
# shared secret for the PC pusher to upload live signals (set INGEST_TOKEN env on Railway)
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "").strip()
# admin password: env var on Railway, else local file
ADMIN_PW_FILE = os.path.join(SITE, "admin_pw.txt")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SESSION_DAYS = 30
_SESSIONS = {}
_ADMIN_SESSIONS = {}

# ---------- storage layer (Postgres if DATABASE_URL, else JSON file) ----------
_USE_DB = bool(DATABASE_URL)
_pool = None

def _db():
    global _pool
    import psycopg2
    from psycopg2.extras import RealDictCursor
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def _init_db():
    if not _USE_DB:
        return
    conn = _db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS members(
        email TEXT PRIMARY KEY,
        first_name TEXT, last_name TEXT, name TEXT,
        country TEXT, phone TEXT,
        salt TEXT, pw TEXT,
        status TEXT DEFAULT 'pending',
        access_until DOUBLE PRECISION DEFAULT 0,
        plan TEXT DEFAULT 'none',
        joined TEXT)""")
    cur.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS access_until DOUBLE PRECISION DEFAULT 0")
    cur.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'none'")
    conn.commit(); cur.close(); conn.close()

def _get_member(email):
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT * FROM members WHERE email=%s", (email,))
        row = cur.fetchone(); cur.close(); conn.close()
        return dict(row) if row else None
    m = _load_file()
    return m.get(email)

def _put_member(email, rec):
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("""INSERT INTO members(email,first_name,last_name,name,country,phone,salt,pw,status,access_until,plan,joined)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(email) DO UPDATE SET status=EXCLUDED.status""",
            (email, rec["first_name"], rec["last_name"], rec["name"], rec["country"],
             rec["phone"], rec["salt"], rec["pw"], rec["status"],
             rec.get("access_until", 0), rec.get("plan", "none"), rec["joined"]))
        conn.commit(); cur.close(); conn.close()
        return
    m = _load_file(); m[email] = rec; _save_file(m)

def _set_status(email, status):
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE members SET status=%s WHERE email=%s", (status, email))
        conn.commit(); cur.close(); conn.close()
        return
    m = _load_file()
    if email in m: m[email]["status"] = status; _save_file(m)

TRIAL_DAYS = 7
MONTH_DAYS = 30
PRICE_USDT = 99

def _grant_access(email, days, plan):
    """Extend a member's access by `days` from now (or from their current expiry if still active)."""
    now = time.time()
    cur_until = 0
    rec = _get_member(email)
    if rec:
        try: cur_until = float(rec.get("access_until") or 0)
        except Exception: cur_until = 0
    base = cur_until if cur_until > now else now
    new_until = base + days * 86400
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE members SET status='approved', access_until=%s, plan=%s WHERE email=%s",
                    (new_until, plan, email))
        conn.commit(); cur.close(); conn.close()
    else:
        m = _load_file()
        if email in m:
            m[email]["status"] = "approved"; m[email]["access_until"] = new_until; m[email]["plan"] = plan
            _save_file(m)
    return new_until

def _access_state(rec):
    """Return (has_access_bool, label, days_left) for a member record."""
    if not rec or rec.get("status") != "approved":
        return (False, "pending", 0)
    now = time.time()
    try: until = float(rec.get("access_until") or 0)
    except Exception: until = 0
    if until <= now:
        return (False, "expired", 0)
    days_left = int((until - now) / 86400) + 1
    plan = rec.get("plan", "none")
    return (True, "trial" if plan == "trial" else "active", days_left)

def _all_members():
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT * FROM members"); rows = cur.fetchall(); cur.close(); conn.close()
        return {r["email"]: dict(r) for r in rows}
    return _load_file()

def _load_file():
    if not os.path.exists(MEMBERS_FILE): return {}
    try: return json.load(open(MEMBERS_FILE, encoding="utf-8"))
    except Exception: return {}

def _save_file(m):
    json.dump(m, open(MEMBERS_FILE, "w", encoding="utf-8"), indent=2)

def _hash(pw, salt): return hashlib.sha256((salt + pw).encode()).hexdigest()

def _new_session(email):
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = (email, time.time() + SESSION_DAYS * 86400)
    return tok

def _session_email(tok):
    if not tok: return None
    rec = _SESSIONS.get(tok)
    if not rec: return None
    email, exp = rec
    if time.time() > exp:
        _SESSIONS.pop(tok, None); return None
    return email

def _admin_pw_hash():
    env = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env:
        return hashlib.sha256(env.encode()).hexdigest()
    if os.path.exists(ADMIN_PW_FILE):
        pw = open(ADMIN_PW_FILE, encoding="utf-8").read().strip()
        if pw and pw != "CHANGE_ME":
            return hashlib.sha256(pw.encode()).hexdigest()
    return None

def _new_admin_session():
    tok = secrets.token_urlsafe(32)
    _ADMIN_SESSIONS[tok] = time.time() + 86400
    return tok

def _is_admin(tok):
    if not tok: return False
    exp = _ADMIN_SESSIONS.get(tok)
    if not exp: return False
    if time.time() > exp:
        _ADMIN_SESSIONS.pop(tok, None); return False
    return True

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

@app.on_event("startup")
def _startup():
    try: _init_db()
    except Exception as e: print("db init note:", e)

@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    first = (body.get("first_name") or "").strip()
    last = (body.get("last_name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    country = (body.get("country") or "").strip()
    phone = (body.get("phone") or "").strip()
    pw = body.get("password") or ""
    if not first or not last:
        return JSONResponse({"ok": False, "error": "First and last name are required"})
    if not EMAIL_RE.match(email):
        return JSONResponse({"ok": False, "error": "Enter a valid email"})
    if not country:
        return JSONResponse({"ok": False, "error": "Country is required"})
    if len(phone) < 5:
        return JSONResponse({"ok": False, "error": "A valid phone number is required"})
    if len(pw) < 6:
        return JSONResponse({"ok": False, "error": "Password must be at least 6 characters"})
    if _get_member(email):
        return JSONResponse({"ok": False, "error": "An account with this email already exists"})
    salt = secrets.token_hex(8)
    rec = {"first_name": first, "last_name": last, "name": (first + " " + last).strip(),
           "country": country, "phone": phone, "salt": salt, "pw": _hash(pw, salt),
           "status": "pending", "joined": time.strftime("%Y-%m-%d %H:%M")}
    _put_member(email, rec)
    tok = _new_session(email)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("k_session", tok, httponly=True, max_age=SESSION_DAYS*86400, samesite="lax")
    return resp

@app.post("/api/auth")
async def auth(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    u = _get_member(email)
    if not u or _hash(pw, u["salt"]) != u["pw"]:
        return JSONResponse({"ok": False, "error": "Wrong email or password"})
    tok = _new_session(email)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("k_session", tok, httponly=True, max_age=SESSION_DAYS*86400, samesite="lax")
    return resp

@app.post("/api/signout")
def signout(k_session: str = Cookie(None)):
    _SESSIONS.pop(k_session, None)
    resp = JSONResponse({"ok": True}); resp.delete_cookie("k_session"); return resp

@app.post("/api/change_password")
async def change_password(request: Request, k_session: str = Cookie(None)):
    """Logged-in member changes their own password. Requires current password."""
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
    body = await request.json()
    cur = body.get("current") or ""
    new = (body.get("new") or "").strip()
    u = _get_member(email)
    if not u or _hash(cur, u["salt"]) != u["pw"]:
        return JSONResponse({"ok": False, "error": "Current password is wrong"})
    if len(new) < 6:
        return JSONResponse({"ok": False, "error": "New password must be at least 6 characters"})
    salt = secrets.token_hex(8)
    new_hash = _hash(new, salt)
    if _USE_DB:
        conn = _db(); c = conn.cursor()
        c.execute("UPDATE members SET salt=%s, pw=%s WHERE email=%s", (salt, new_hash, email))
        conn.commit(); c.close(); conn.close()
    else:
        m = _load_file(); m[email]["salt"] = salt; m[email]["pw"] = new_hash; _save_file(m)
    return {"ok": True, "msg": "Password changed"}

@app.get("/api/me")
def me(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email: return JSONResponse({"ok": False}, status_code=401)
    u = _get_member(email) or {}
    has_access, label, days_left = _access_state(u)
    return {"ok": True, "email": email, "name": u.get("name", ""),
            "status": u.get("status", "pending"), "joined": u.get("joined", ""),
            "has_access": has_access, "access_label": label, "days_left": days_left,
            "price_usdt": PRICE_USDT}

@app.get("/api/results")
def results(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email: return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, label, days_left = _access_state(u)
    if not has_access:
        return JSONResponse({"error": label}, status_code=403)
    if not os.path.exists(RESULTS_FILE):
        return {"trades": [], "total": 0, "win_rate": 0}
    return json.load(open(RESULTS_FILE, encoding="utf-8"))

@app.get("/api/signals")
def signals(k_session: str = Cookie(None)):
    """Live signals for members. Gated by the same access rule as results."""
    email = _session_email(k_session)
    if not email: return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, label, days_left = _access_state(u)
    if not has_access:
        return JSONResponse({"error": label}, status_code=403)
    if not os.path.exists(SIGNALS_FILE):
        return {"signals": [], "price": None, "trend": None, "news_status": None,
                "next_event": None, "generated_utc": None, "stale": True}
    try:
        data = json.load(open(SIGNALS_FILE, encoding="utf-8"))
    except Exception:
        return {"signals": [], "stale": True}
    # mark stale if the feed hasn't been refreshed in > 20 min (engine writes every 5m candle)
    try:
        age = time.time() - os.path.getmtime(SIGNALS_FILE)
        data["stale"] = age > 1200
        data["age_seconds"] = int(age)
    except Exception:
        data["stale"] = False
    return data

@app.post("/api/ingest_signals")
async def ingest_signals(request: Request):
    """One-way push from the PC engine. Protected by INGEST_TOKEN.
    SAFETY: write-only sink. Stores the latest zones snapshot; never executes anything."""
    if not INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "ingest disabled (no token set)"}, status_code=403)
    auth = request.headers.get("x-ingest-token", "")
    if auth != INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    # only keep the safe public fields; never trust arbitrary keys
    hist = body.get("history") or []
    if not isinstance(hist, list): hist = []
    safe = {
        "generated_utc": body.get("generated_utc"),
        "price": body.get("price"),
        "trend": body.get("trend"),
        "news_status": body.get("news_status"),
        "next_event": body.get("next_event"),
        "signals": body.get("signals") or [],
        "history": hist[-100:],
    }
    try:
        json.dump(safe, open(SIGNALS_FILE, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "count": len(safe["signals"])}

@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    pw = (body.get("password") or "").strip()
    h = _admin_pw_hash()
    if h is None:
        return JSONResponse({"ok": False, "error": "admin password not set"})
    if hashlib.sha256(pw.encode()).hexdigest() == h:
        tok = _new_admin_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("k_admin", tok, httponly=True, max_age=86400, samesite="lax")
        return resp
    return JSONResponse({"ok": False, "error": "wrong password"})

@app.post("/api/admin/signout")
def admin_signout(k_admin: str = Cookie(None)):
    _ADMIN_SESSIONS.pop(k_admin, None)
    resp = JSONResponse({"ok": True}); resp.delete_cookie("k_admin"); return resp

@app.get("/api/admin/members")
def admin_members(k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    members = _all_members()
    out = []
    for email, u in members.items():
        has_access, label, days_left = _access_state(u)
        out.append({"email": email, "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""), "country": u.get("country", ""),
                    "phone": u.get("phone", ""), "status": u.get("status", "pending"),
                    "access_label": label, "days_left": days_left, "plan": u.get("plan", "none"),
                    "joined": u.get("joined", "")})
    out.sort(key=lambda x: (x["status"] != "pending", x["joined"]))
    return {"ok": True, "members": out,
            "pending": sum(1 for m in out if m["status"] == "pending"),
            "active": sum(1 for m in out if m["access_label"] in ("trial", "active")),
            "expired": sum(1 for m in out if m["access_label"] == "expired")}

@app.post("/api/admin/set_status")
async def admin_set_status(request: Request, k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    action = body.get("action")
    if not _get_member(email):
        return JSONResponse({"ok": False, "error": "no such member"})
    if action == "approve_trial":
        _grant_access(email, TRIAL_DAYS, "trial")
        return {"ok": True, "email": email, "msg": "7-day trial started"}
    elif action == "extend_month":
        _grant_access(email, MONTH_DAYS, "paid")
        return {"ok": True, "email": email, "msg": "30 days added (paid)"}
    elif action == "revoke":
        _set_status(email, "pending")
        return {"ok": True, "email": email, "msg": "access revoked"}
    elif action == "reset_password":
        newpw = (body.get("new_password") or "").strip()
        if len(newpw) < 6:
            return JSONResponse({"ok": False, "error": "new password must be at least 6 characters"})
        rec = _get_member(email)
        salt = secrets.token_hex(8)
        rec["salt"] = salt
        rec["pw"] = _hash(newpw, salt)
        _put_member(email, rec)
        # _put_member's UPSERT only updates status on conflict; force the pw/salt write directly
        if _USE_DB:
            conn = _db(); cur = conn.cursor()
            cur.execute("UPDATE members SET salt=%s, pw=%s WHERE email=%s", (salt, rec["pw"], email))
            conn.commit(); cur.close(); conn.close()
        else:
            m = _load_file(); m[email]["salt"] = salt; m[email]["pw"] = rec["pw"]; _save_file(m)
        return {"ok": True, "email": email, "msg": "password reset"}
    return JSONResponse({"ok": False, "error": "bad action"})

@app.get("/api/health")
def health():
    """Non-sensitive diagnostic: which storage backend is live, and member count."""
    mode = "postgres" if _USE_DB else "file(ephemeral)"
    try:
        n = len(_all_members())
    except Exception as e:
        n = -1
    return {"storage": mode, "members": n, "db_url_set": bool(DATABASE_URL)}

@app.get("/")
def home(): return FileResponse(os.path.join(SITE, "index.html"))

@app.get("/public_results.json")
def pub_results(): return FileResponse(RESULTS_FILE)

@app.get("/i18n.js")
def i18n_js(): return FileResponse(os.path.join(SITE, "i18n.js"), media_type="application/javascript")

@app.get("/login")
def login_page(): return FileResponse(os.path.join(SITE, "login.html"))

@app.get("/signup")
def signup_page(): return FileResponse(os.path.join(SITE, "signup.html"))

@app.get("/connect")
def connect_page(k_session: str = Cookie(None)):
    if not _session_email(k_session): return RedirectResponse("/login")
    return FileResponse(os.path.join(SITE, "connect.html"))

@app.get("/members")
def members_page(k_session: str = Cookie(None)):
    if not _session_email(k_session): return RedirectResponse("/login")
    return FileResponse(os.path.join(SITE, "members.html"))

@app.get("/admin")
def admin_page(): return FileResponse(os.path.join(SITE, "admin.html"))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
