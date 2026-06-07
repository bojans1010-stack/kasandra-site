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
        joined TEXT)""")
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
        cur.execute("""INSERT INTO members(email,first_name,last_name,name,country,phone,salt,pw,status,joined)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(email) DO UPDATE SET status=EXCLUDED.status""",
            (email, rec["first_name"], rec["last_name"], rec["name"], rec["country"],
             rec["phone"], rec["salt"], rec["pw"], rec["status"], rec["joined"]))
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

@app.get("/api/me")
def me(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email: return JSONResponse({"ok": False}, status_code=401)
    u = _get_member(email) or {}
    return {"ok": True, "email": email, "name": u.get("name", ""),
            "status": u.get("status", "pending"), "joined": u.get("joined", "")}

@app.get("/api/results")
def results(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email: return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    if u.get("status") != "approved":
        return JSONResponse({"error": "account pending approval"}, status_code=403)
    if not os.path.exists(RESULTS_FILE):
        return {"trades": [], "total": 0, "win_rate": 0}
    return json.load(open(RESULTS_FILE, encoding="utf-8"))

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
        out.append({"email": email, "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""), "country": u.get("country", ""),
                    "phone": u.get("phone", ""), "status": u.get("status", "pending"),
                    "joined": u.get("joined", "")})
    out.sort(key=lambda x: (x["status"] != "pending", x["joined"]))
    return {"ok": True, "members": out,
            "pending": sum(1 for m in out if m["status"] == "pending"),
            "approved": sum(1 for m in out if m["status"] == "approved")}

@app.post("/api/admin/set_status")
async def admin_set_status(request: Request, k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    status = body.get("status")
    if status not in ("approved", "pending", "rejected"):
        return JSONResponse({"ok": False, "error": "bad status"})
    if not _get_member(email):
        return JSONResponse({"ok": False, "error": "no such member"})
    _set_status(email, status)
    return {"ok": True, "email": email, "status": status}

@app.get("/")
def home(): return FileResponse(os.path.join(SITE, "index.html"))

@app.get("/public_results.json")
def pub_results(): return FileResponse(RESULTS_FILE)

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
