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
    cur.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE")
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

def _set_expiry(email, date_str, plan):
    """Set an absolute expiry date (YYYY-MM-DD, end of that day UTC)."""
    import calendar
    try:
        ts = calendar.timegm(time.strptime(date_str + " 23:59", "%Y-%m-%d %H:%M"))
    except Exception:
        return None
    approved = "approved" if ts > time.time() else "pending"
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE members SET status=%s, access_until=%s, plan=%s WHERE email=%s",
                    (approved, ts, plan, email))
        conn.commit(); cur.close(); conn.close()
    else:
        m = _load_file()
        if email in m:
            m[email]["status"] = approved; m[email]["access_until"] = ts; m[email]["plan"] = plan
            _save_file(m)
    return ts

FREE_PLANS = ("free", "comp", "gift")

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
    label = "trial" if plan == "trial" else "free" if plan in FREE_PLANS else "active"
    return (True, label, days_left)

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

def _member_is_admin(rec):
    """True if a member account is flagged as admin."""
    if not rec: return False
    v = rec.get("is_admin")
    return v is True or v == 1 or str(v).lower() == "true"

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
    # single-source policy: members see the NY Pipeline chart system only.
    # The legacy engine still pushes; its payloads are acknowledged but ignored
    # (its own processes are never touched).
    if body.get("engine") != "nypipeline":
        return JSONResponse({"ok": False, "error": "legacy engine payload ignored"}, status_code=409)
    # only keep the safe public fields; never trust arbitrary keys
    hist = body.get("history") or []
    if not isinstance(hist, list): hist = []
    candles = body.get("candles") or []
    if not isinstance(candles, list): candles = []
    open_trades = body.get("open_trades") or []
    if not isinstance(open_trades, list): open_trades = []
    safe = {
        "generated_utc": body.get("generated_utc"),
        "price": body.get("price"),
        "trend": body.get("trend"),
        "news_status": body.get("news_status"),
        "next_event": body.get("next_event"),
        "signals": body.get("signals") or [],
        "history": hist[-100:],
        "open_trades": open_trades[:8],
        "candles": candles[-400:],
    }
    try:
        json.dump(safe, open(SIGNALS_FILE, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "count": len(safe["signals"])}

CHART_FILE = os.path.join(SITE, "live_chart.png")
TICK_FILE = os.path.join(SITE, "tick.json")

@app.post("/api/ingest_tick")
async def ingest_tick(request: Request):
    """High-frequency price tick from the PC. Tiny file write, shared across workers."""
    if not INGEST_TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    if request.headers.get("x-ingest-token", "") != INGEST_TOKEN:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        body = await request.json()
        price = float(body.get("price"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    try:
        with open(TICK_FILE, "w") as f:
            json.dump({"price": price, "t": time.time()}, f)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}

@app.get("/api/tick")
def tick(k_session: str = Cookie(None)):
    """Live price tick for members. Tiny payload, poll-friendly."""
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, _, _ = _access_state(u)
    if not has_access:
        return JSONResponse({"error": "no access"}, status_code=403)
    try:
        d = json.load(open(TICK_FILE))
        return {"price": d.get("price"), "age": round(time.time() - d.get("t", 0), 1)}
    except Exception:
        return {"price": None, "age": None}

@app.post("/api/ingest_chart")
async def ingest_chart(request: Request):
    """One-way push of the live chart PNG from the PC. Protected by INGEST_TOKEN.
    SAFETY: write-only sink, image bytes only, size-capped."""
    if not INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "ingest disabled (no token set)"}, status_code=403)
    if request.headers.get("x-ingest-token", "") != INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    body = await request.body()
    if not body or len(body) > 4_000_000:
        return JSONResponse({"ok": False, "error": "bad size"}, status_code=400)
    if not body.startswith(b"\x89PNG"):
        return JSONResponse({"ok": False, "error": "not a png"}, status_code=400)
    try:
        with open(CHART_FILE, "wb") as f:
            f.write(body)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "bytes": len(body)}

@app.get("/api/chart.png")
def chart_png(k_session: str = Cookie(None)):
    """Live chart image for members. Same access gate as signals."""
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, label, _ = _access_state(u)
    if not has_access:
        return JSONResponse({"error": label}, status_code=403)
    if not os.path.exists(CHART_FILE):
        return JSONResponse({"error": "no chart yet"}, status_code=404)
    age = int(time.time() - os.path.getmtime(CHART_FILE))
    resp = FileResponse(CHART_FILE, media_type="image/png")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Chart-Age"] = str(age)
    return resp

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

@app.post("/api/admin/login_user")
async def admin_login_user(request: Request):
    """Admin login via a member's own email + password, if their account is_admin."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    u = _get_member(email)
    if not u or _hash(pw, u["salt"]) != u["pw"]:
        return JSONResponse({"ok": False, "error": "wrong email or password"})
    if not _member_is_admin(u):
        return JSONResponse({"ok": False, "error": "this account is not an admin"})
    tok = _new_admin_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("k_admin", tok, httponly=True, max_age=86400, samesite="lax")
    return resp

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
    now = time.time()
    for email, u in members.items():
        has_access, label, days_left = _access_state(u)
        try: until = float(u.get("access_until") or 0)
        except Exception: until = 0
        until_str = time.strftime("%Y-%m-%d", time.gmtime(until)) if until > 0 else ""
        out.append({"email": email, "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""), "country": u.get("country", ""),
                    "phone": u.get("phone", ""), "status": u.get("status", "pending"),
                    "access_label": label, "days_left": days_left, "plan": u.get("plan", "none"),
                    "joined": u.get("joined", ""), "access_until": until_str,
                    "is_admin": _member_is_admin(u)})
    out.sort(key=lambda x: (x["status"] != "pending", x["joined"]))
    paid = sum(1 for m in out if m["access_label"] == "active")
    trial = sum(1 for m in out if m["access_label"] == "trial")
    free = sum(1 for m in out if m["access_label"] == "free")
    expiring = sum(1 for m in out if m["access_label"] in ("trial", "active", "free") and 0 < m["days_left"] <= 7)
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
    new7 = sum(1 for m in out if (m.get("joined") or "")[:10] >= cutoff)
    return {"ok": True, "members": out,
            "pending": sum(1 for m in out if m["status"] == "pending"),
            "active": trial + paid + free,
            "expired": sum(1 for m in out if m["access_label"] == "expired"),
            "paid": paid, "trial": trial, "free": free,
            "expiring": expiring, "new7": new7, "mrr": paid * PRICE_USDT}

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
    elif action == "gift_week":
        _grant_access(email, 7, "free")
        return {"ok": True, "email": email, "msg": "1 week gifted (free)"}
    elif action == "gift_month":
        _grant_access(email, 30, "free")
        return {"ok": True, "email": email, "msg": "1 month gifted (free)"}
    elif action == "grant":
        try:
            days = int(body.get("days"))
        except Exception:
            return JSONResponse({"ok": False, "error": "days must be a number"})
        plan = (body.get("plan") or "paid").strip().lower()
        if plan not in ("paid", "free", "trial", "comp"):
            plan = "paid"
        if days < 1 or days > 3650:
            return JSONResponse({"ok": False, "error": "days out of range"})
        nu = _grant_access(email, days, plan)
        return {"ok": True, "email": email, "msg": f"+{days} days ({plan})",
                "until": time.strftime("%Y-%m-%d", time.gmtime(nu))}
    elif action == "set_expiry":
        plan = (body.get("plan") or "paid").strip().lower()
        ts = _set_expiry(email, (body.get("date") or "").strip(), plan)
        if ts is None:
            return JSONResponse({"ok": False, "error": "bad date (use YYYY-MM-DD)"})
        return {"ok": True, "email": email, "msg": "expiry set",
                "until": time.strftime("%Y-%m-%d", time.gmtime(ts))}
    elif action == "revoke":
        _set_status(email, "pending")
        return {"ok": True, "email": email, "msg": "access revoked"}
    elif action in ("make_admin", "remove_admin"):
        val = (action == "make_admin")
        if _USE_DB:
            conn = _db(); cur = conn.cursor()
            cur.execute("UPDATE members SET is_admin=%s WHERE email=%s", (val, email))
            conn.commit(); cur.close(); conn.close()
        else:
            m = _load_file()
            if email in m: m[email]["is_admin"] = val; _save_file(m)
        return {"ok": True, "email": email, "msg": ("now an admin" if val else "admin removed")}
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

SITE_VERSION = "status-banner-1"   # bump on notable deploys; check at /api/version

def _trade_points(r):
    """Points result of one closed signal (thirds at TP1/2/3, BE after TP1)."""
    e, sl, t1, t2, t3 = r.get("entry"), r.get("sl"), r.get("tp1"), r.get("tp2"), r.get("tp3")
    o = r.get("outcome")
    if None not in (e, sl, t1, t2, t3):
        d1, d2, d3, ds = abs(t1 - e), abs(t2 - e), abs(t3 - e), abs(sl - e)
        return {"TP3": (d1 + d2 + d3) / 3, "TP2": (d1 + d2) / 3, "TP1": d1 / 3,
                "BE": 0.0}.get(o, -ds if o == "SL" else 0.0)
    return {"TP3": 11.7, "TP2": 5.0, "TP1": 1.7, "BE": 0.0, "SL": -10.0}.get(o, 0.0)

@app.get("/api/public_stats")
def public_stats():
    """Public homepage stats computed from the NY Pipeline signal history.
    Closed signals only — no live levels are exposed."""
    hist = []
    if os.path.exists(SIGNALS_FILE):
        try:
            hist = (json.load(open(SIGNALS_FILE, encoding="utf-8")) or {}).get("history") or []
        except Exception:
            hist = []
    trades, pips_total, wins, sl_count, tp3_count = [], 0.0, 0, 0, 0
    for r in hist:
        o = r.get("outcome")
        pts = _trade_points(r)
        pips = round(pts * 10)
        pips_total += pts * 10
        if o == "SL":
            sl_count += 1
        else:
            wins += 1
            if o == "TP3": tp3_count += 1
        cu = (r.get("closed_utc") or "")
        exit_price = r.get("tp3") if o == "TP3" else r.get("sl")
        trades.append({
            "date": cu[:10], "time": cu[11:16],
            "side": r.get("side", ""),
            "entry": r.get("entry", ""), "exit": exit_price if exit_price is not None else "",
            "pips": pips, "result": "LOSS" if o == "SL" else "WIN",
            "outcome": o,
        })
    total = len(trades)
    return {
        "total": total, "wins": wins,
        "win_rate": round(wins * 100 / total) if total else 0,
        "sl_count": sl_count, "tp3_count": tp3_count,
        "pips_total": round(pips_total),
        "usd_1lot": round(pips_total * 10),
        "trades": list(reversed(trades))[:100],
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    }

@app.get("/api/version")
def version():
    return {"version": SITE_VERSION}

@app.get("/api/health")
def health():
    """Non-sensitive diagnostic: which storage backend is live, and member count."""
    mode = "postgres" if _USE_DB else "file(ephemeral)"
    try:
        n = len(_all_members())
    except Exception as e:
        n = -1
    return {"storage": mode, "members": n, "db_url_set": bool(DATABASE_URL)}

def _page(name, media_type=None):
    """Serve a site file with no-cache so browsers (mobile especially) always revalidate."""
    resp = FileResponse(os.path.join(SITE, name), media_type=media_type)
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp

@app.get("/")
def home(): return _page("index.html")

@app.post("/api/ingest_results")
async def ingest_results(request: Request):
    """One-way push of the public results snapshot from the PC. Protected by INGEST_TOKEN.
    Writes public_results.json so both the homepage and members results update with no redeploy."""
    if not INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "ingest disabled (no token set)"}, status_code=403)
    if request.headers.get("x-ingest-token", "") != INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    trades = body.get("trades")
    if not isinstance(trades, list):
        return JSONResponse({"ok": False, "error": "trades must be a list"}, status_code=400)
    safe = {
        "trades": trades,
        "total": body.get("total", len(trades)),
        "win_rate": body.get("win_rate", 0),
        "updated": body.get("updated", ""),
    }
    # carry through any extra display fields the homepage uses, but only known keys
    for k in ("roi_pct", "max_dd", "period"):
        if k in body:
            safe[k] = body[k]
    try:
        json.dump(safe, open(RESULTS_FILE, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "total": safe["total"]}

@app.get("/api/admin/overview")
def admin_overview(k_admin: str = Cookie(None)):
    """Everything the admin needs to see at a glance, no member account needed:
    trade results + stats, live signals, signal history, and member counts."""
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    out = {"ok": True}
    # --- trade results: computed from the NY Pipeline signal history (same as homepage) ---
    out["results"] = public_stats()
    # --- live signals + history ---
    sig = {"signals": [], "history": [], "price": None, "trend": None,
           "news_status": None, "next_event": None, "generated_utc": None, "stale": True}
    if os.path.exists(SIGNALS_FILE):
        try:
            sig = json.load(open(SIGNALS_FILE, encoding="utf-8"))
            age = time.time() - os.path.getmtime(SIGNALS_FILE)
            sig["stale"] = age > 1200
        except Exception: pass
    out["signals"] = sig.get("signals", [])
    out["history"] = sig.get("history", [])
    out["candles"] = sig.get("candles", [])
    out["open_trades"] = sig.get("open_trades", [])
    out["market"] = {"price": sig.get("price"), "trend": sig.get("trend"),
                     "news_status": sig.get("news_status"), "next_event": sig.get("next_event"),
                     "stale": sig.get("stale", True), "generated_utc": sig.get("generated_utc")}
    # --- history outcome stats ---
    hist = sig.get("history", []) or []
    cnt = {"TP3": 0, "TP2": 0, "TP1": 0, "BE": 0, "SL": 0}
    for h in hist:
        o = h.get("outcome")
        if o in cnt: cnt[o] += 1
    wins = cnt["TP1"] + cnt["TP2"] + cnt["TP3"] + cnt["BE"]
    htotal = len(hist)
    out["history_stats"] = {**cnt, "total": htotal,
                            "hit_rate": round(wins / htotal * 100) if htotal else 0}
    # --- member counts ---
    members = _all_members()
    pend = act = exp = trial = paid = 0
    for u in members.values():
        has, label, _ = _access_state(u)
        if label == "pending": pend += 1
        elif label == "trial": trial += 1; act += 1
        elif label == "active": paid += 1; act += 1
        elif label == "expired": exp += 1
    out["member_stats"] = {"total": len(members), "pending": pend, "active": act,
                           "trial": trial, "paid": paid, "expired": exp,
                           "mrr_usdt": paid * PRICE_USDT}
    return out

@app.get("/public_results.json")
def pub_results(): return FileResponse(RESULTS_FILE)

@app.get("/i18n.js")
def i18n_js(): return _page("i18n.js", media_type="application/javascript")

@app.get("/login")
def login_page(): return _page("login.html")

@app.get("/signup")
def signup_page(): return _page("signup.html")

@app.get("/connect")
def connect_page(k_session: str = Cookie(None)):
    if not _session_email(k_session): return RedirectResponse("/login")
    return _page("connect.html")

@app.get("/members")
def members_page(k_session: str = Cookie(None)):
    if not _session_email(k_session): return RedirectResponse("/login")
    return _page("members.html")

@app.get("/risk")
def risk_page(k_session: str = Cookie(None)):
    if not _session_email(k_session): return RedirectResponse("/login")
    return _page("risk.html")

@app.get("/admin")
def admin_page(): return _page("admin.html")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
