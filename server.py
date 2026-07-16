# -*- coding: utf-8 -*-
"""Kasandra Technologies - members site backend (Postgres-ready).
SAFETY: completely separate from the trading system. Reads results one-way only.

Storage: uses PostgreSQL when DATABASE_URL is set (Railway), otherwise falls back
to a local JSON file (members.json) for local testing. Same API either way.
"""
from fastapi import FastAPI, Request, Cookie
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
import json, os, hashlib, hmac, secrets, time, re, base64, urllib.request, urllib.error, threading

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
# Kasandra AI assistant (Claude). Set ANTHROPIC_API_KEY on Railway to enable.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-4-5").strip()
CHAT_ENABLED = bool(ANTHROPIC_API_KEY)
_chat_rate = {}   # ip -> [timestamps] simple per-IP throttle
# Cryptomus crypto payments (set these env vars on Railway; the code never hardcodes them)
CRYPTOMUS_MERCHANT = os.environ.get("CRYPTOMUS_MERCHANT", "").strip()
CRYPTOMUS_API_KEY = os.environ.get("CRYPTOMUS_API_KEY", "").strip()
SITE_URL_PUBLIC = os.environ.get("SITE_URL_PUBLIC", "https://kasandra.app").strip().rstrip("/")
PAY_ENABLED = bool(CRYPTOMUS_MERCHANT and CRYPTOMUS_API_KEY)
PAYMENTS_FILE = os.path.join(SITE, "payments.json")
# Stripe card payments (hosted payment link + webhook). The link URL is public;
# STRIPE_WEBHOOK_SECRET (whsec_...) is the secret you set on Railway for auto-grant.
STRIPE_PAYMENT_LINK = os.environ.get("STRIPE_PAYMENT_LINK", "https://buy.stripe.com/aFa6oHfVn3R86FabfHfAc0N").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_ENABLED = bool(STRIPE_PAYMENT_LINK)          # show the card button
STRIPE_AUTO = bool(STRIPE_PAYMENT_LINK and STRIPE_WEBHOOK_SECRET)   # auto-grant on webhook

# Private Telegram signals channel (paid members only). The invite link is served
# only to authenticated members with active access — never embedded in public HTML.
TELEGRAM_INVITE_LINK = os.environ.get("TELEGRAM_INVITE_LINK", "https://t.me/+2r11N5pC8LcwZjlk").strip()

# --- Self-hosted crypto payments (no processor; direct USDT to our own wallets) ---
# PUBLIC receiving addresses only (safe to expose). A background watcher polls the
# chains and auto-grants membership when a payment with the exact unique amount lands.
# The server never holds keys — it only READS public blockchain data.
USDT_TRON_ADDR = os.environ.get("USDT_TRON_ADDR", "TYjkhBgaKFHSM1w4knFt1FfhLEA8dLqxV6").strip()
USDT_ERC20_ADDR = os.environ.get("USDT_ERC20_ADDR", "0xD7C168ABDe6AEc0aF3f966956478DbB44f2B018D").strip()
TRONGRID_KEY = os.environ.get("TRONGRID_KEY", "").strip()      # optional, raises rate limit
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_KEY", "").strip()    # required to enable ERC20 watching
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_ERC20_CONTRACT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
CRYPTO_TRON_ON = bool(USDT_TRON_ADDR)
CRYPTO_ERC20_ON = bool(USDT_ERC20_ADDR and ETHERSCAN_KEY)
SELFCRYPTO_ENABLED = CRYPTO_TRON_ON or CRYPTO_ERC20_ON
ORDER_LIFETIME = 7200          # a pending crypto order is valid for 2 hours

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
    cur.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS ref_code TEXT")
    cur.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS referred_by TEXT")
    cur.execute("""CREATE TABLE IF NOT EXISTS commissions(
        id SERIAL PRIMARY KEY, referrer TEXT, referred TEXT,
        amount DOUBLE PRECISION, created TEXT, status TEXT DEFAULT 'pending')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payments(
        order_id TEXT PRIMARY KEY, email TEXT, amount DOUBLE PRECISION,
        status TEXT, created TEXT, fulfilled BOOLEAN DEFAULT FALSE)""")
    cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS network TEXT")
    cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS txid TEXT")
    cur.execute("""CREATE TABLE IF NOT EXISTS chat_logs(
        id SERIAL PRIMARY KEY, session TEXT, ip TEXT, role TEXT, content TEXT, created TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)""")
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

TRIAL_DAYS = 3
MONTH_DAYS = 30
PRICE_USDT = 99
REF_PCT = 30                                   # affiliate commission: 30% of each paid month
REF_COMMISSION = round(PRICE_USDT * REF_PCT / 100)   # = 30 USDT per paid month, per referral
COMMISSIONS_FILE = os.path.join(SITE, "commissions.json")   # JSON-mode fallback store

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
    # An admin-set expiry always marks a real member; access is decided by access_until
    # vs now. A past date => "expired" (shows the renew/Pay button), NOT "pending"
    # (which hides it and reads as an unreviewed signup).
    approved = "approved"
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

# ---------- affiliate / referral system ----------
def _gen_ref_code():
    import string
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))

def _member_by_ref(code):
    if not code:
        return None
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT * FROM members WHERE ref_code=%s", (code,))
        row = cur.fetchone(); cur.close(); conn.close()
        return dict(row) if row else None
    for e, m in _load_file().items():
        if m.get("ref_code") == code:
            return {**m, "email": e}
    return None

def _set_member_field(email, field, val):
    # `field` is always an internal literal, never user input -> safe to interpolate
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute(f"UPDATE members SET {field}=%s WHERE email=%s", (val, email))
        conn.commit(); cur.close(); conn.close()
    else:
        m = _load_file()
        if email in m:
            m[email][field] = val; _save_file(m)

def _uniq_ref_code():
    for _ in range(12):
        c = _gen_ref_code()
        if not _member_by_ref(c):
            return c
    return _gen_ref_code()

def _ensure_ref_code(email):
    m = _get_member(email)
    if not m:
        return None
    if m.get("ref_code"):
        return m["ref_code"]
    code = _uniq_ref_code()
    _set_member_field(email, "ref_code", code)
    return code

def _load_commissions():
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT * FROM commissions ORDER BY id")
        rows = cur.fetchall(); cur.close(); conn.close()
        return [dict(r) for r in rows]
    if not os.path.exists(COMMISSIONS_FILE):
        return []
    try:
        return json.load(open(COMMISSIONS_FILE, encoding="utf-8"))
    except Exception:
        return []

def _add_commission(referrer_email, referred_email, amount):
    created = time.strftime("%Y-%m-%d %H:%M")
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("INSERT INTO commissions(referrer,referred,amount,created,status) VALUES(%s,%s,%s,%s,'pending')",
                    (referrer_email, referred_email, amount, created))
        conn.commit(); cur.close(); conn.close()
    else:
        c = _load_commissions()
        nid = (max([x.get("id", 0) for x in c]) + 1) if c else 1
        c.append({"id": nid, "referrer": referrer_email, "referred": referred_email,
                  "amount": amount, "created": created, "status": "pending"})
        json.dump(c, open(COMMISSIONS_FILE, "w", encoding="utf-8"), indent=2)

def _settle_commissions(referrer_email):
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE commissions SET status='settled' WHERE referrer=%s AND status='pending'", (referrer_email,))
        n = cur.rowcount; conn.commit(); cur.close(); conn.close()
        return n
    c = _load_commissions(); n = 0
    for x in c:
        if x.get("referrer") == referrer_email and x.get("status") == "pending":
            x["status"] = "settled"; n += 1
    json.dump(c, open(COMMISSIONS_FILE, "w", encoding="utf-8"), indent=2)
    return n

def _accrue_referral(referred_email):
    """When a referred member gets a PAID month, credit their referrer a 30% commission."""
    m = _get_member(referred_email)
    if not m or not m.get("referred_by"):
        return
    ref = _member_by_ref(m["referred_by"])
    if not ref or ref.get("email") == referred_email:
        return
    _add_commission(ref["email"], referred_email, REF_COMMISSION)

# ---------- crypto payments (Cryptomus) ----------
def _load_payments():
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT * FROM payments ORDER BY created DESC")
        rows = cur.fetchall(); cur.close(); conn.close()
        return [dict(r) for r in rows]
    if not os.path.exists(PAYMENTS_FILE):
        return []
    try:
        return sorted(json.load(open(PAYMENTS_FILE, encoding="utf-8")),
                      key=lambda p: p.get("created", ""), reverse=True)
    except Exception:
        return []

def _get_payment(order_id):
    for p in _load_payments():
        if p.get("order_id") == order_id:
            return p
    return None

def _upsert_payment(order_id, email, amount, status, fulfilled):
    created = time.strftime("%Y-%m-%d %H:%M")
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("""INSERT INTO payments(order_id,email,amount,status,created,fulfilled)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(order_id) DO UPDATE SET status=EXCLUDED.status, fulfilled=EXCLUDED.fulfilled""",
            (order_id, email, amount, status, created, fulfilled))
        conn.commit(); cur.close(); conn.close()
    else:
        ps = _load_payments()
        for p in ps:
            if p.get("order_id") == order_id:
                p["status"] = status; p["fulfilled"] = fulfilled
                json.dump(ps, open(PAYMENTS_FILE, "w", encoding="utf-8"), indent=2)
                return
        ps.insert(0, {"order_id": order_id, "email": email, "amount": amount,
                      "status": status, "created": created, "fulfilled": fulfilled})
        json.dump(ps, open(PAYMENTS_FILE, "w", encoding="utf-8"), indent=2)

def _cmus_sign(data_dict):
    """Cryptomus signature: md5( base64(php_json_encode(data)) + API_KEY ). Match PHP json_encode:
    compact separators + escaped forward slashes."""
    raw = json.dumps(data_dict, separators=(',', ':')).replace('/', '\\/')
    return hashlib.md5((base64.b64encode(raw.encode()).decode() + CRYPTOMUS_API_KEY).encode()).hexdigest()

def _cryptomus_create_invoice(order_id, amount, email):
    payload = {
        "amount": str(amount), "currency": "USD", "order_id": order_id,
        "url_callback": SITE_URL_PUBLIC + "/api/pay/webhook",
        "url_return": SITE_URL_PUBLIC + "/members",
        "url_success": SITE_URL_PUBLIC + "/members",
        "additional_data": email, "lifetime": 3600, "subtract": "100",
    }
    raw = json.dumps(payload, separators=(',', ':')).replace('/', '\\/')
    sign = hashlib.md5((base64.b64encode(raw.encode()).decode() + CRYPTOMUS_API_KEY).encode()).hexdigest()
    req = urllib.request.Request("https://api.cryptomus.com/v1/payment", data=raw.encode(), method="POST",
        headers={"merchant": CRYPTOMUS_MERCHANT, "sign": sign, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def _verify_webhook(data):
    sign = data.get("sign")
    if not sign:
        return False
    d = {k: v for k, v in data.items() if k != "sign"}
    return secrets.compare_digest(_cmus_sign(d), str(sign))

def _fulfill_payment(order_id, email, amount):
    """Grant a paid month + accrue affiliate commission, exactly once per order."""
    p = _get_payment(order_id)
    if p and p.get("fulfilled"):
        return False
    _grant_access(email, MONTH_DAYS, "paid")
    _accrue_referral(email)
    _upsert_payment(order_id, email, amount, "paid", True)
    return True

# ---------- self-hosted crypto payment watcher (direct USDT to our wallets) ----------
def _created_ts(p):
    try: return time.mktime(time.strptime(p.get("created", ""), "%Y-%m-%d %H:%M"))
    except Exception: return 0.0

def _pending_crypto_orders():
    """Live (unpaid, unexpired) crypto orders."""
    now = time.time()
    out = []
    for p in _load_payments():
        if p.get("status") == "pending" and not p.get("fulfilled") and p.get("network"):
            if now - _created_ts(p) <= ORDER_LIFETIME:
                out.append(p)
    return out

def _unique_crypto_amount(network):
    """Smallest 99.00X amount (3 decimals) not used by another live order on this network,
    so an incoming transfer maps to exactly one buyer."""
    used = set()
    for p in _pending_crypto_orders():
        if p.get("network") == network:
            try: used.add(round(float(p["amount"]), 3))
            except Exception: pass
    for i in range(1, 1000):
        amt = round(PRICE_USDT + i / 1000.0, 3)   # 99.001 .. 99.999
        if amt not in used:
            return amt
    return round(PRICE_USDT + 0.001, 3)

def _create_crypto_order(order_id, email, amount, network):
    created = time.strftime("%Y-%m-%d %H:%M")
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("""INSERT INTO payments(order_id,email,amount,status,created,fulfilled,network)
            VALUES(%s,%s,%s,'pending',%s,FALSE,%s) ON CONFLICT(order_id) DO NOTHING""",
            (order_id, email, amount, created, network))
        conn.commit(); cur.close(); conn.close()
    else:
        ps = _load_payments()
        ps.insert(0, {"order_id": order_id, "email": email, "amount": amount, "status": "pending",
                      "created": created, "fulfilled": False, "network": network, "txid": ""})
        json.dump(ps, open(PAYMENTS_FILE, "w", encoding="utf-8"), indent=2)

def _set_txid(order_id, txid):
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE payments SET txid=%s WHERE order_id=%s", (txid, order_id))
        conn.commit(); cur.close(); conn.close()
    else:
        ps = _load_payments()
        for p in ps:
            if p.get("order_id") == order_id: p["txid"] = txid
        json.dump(ps, open(PAYMENTS_FILE, "w", encoding="utf-8"), indent=2)

def _txid_seen(txid):
    if not txid: return False
    for p in _load_payments():
        if p.get("txid") == txid: return True
    return False

def _http_get_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def _txid_fulfilled(txid):
    """True only if this txid already fulfilled an order — so a still-unmatched txid
    stays retryable on later polls (e.g. once the buyer re-opens checkout)."""
    if not txid: return False
    for p in _load_payments():
        if p.get("txid") == txid and p.get("fulfilled"):
            return True
    return False

def _match_incoming(network, amount, txid):
    """A confirmed USDT transfer landed. Match it to one pending order and fulfill, once per txid."""
    if not txid or _txid_fulfilled(txid):
        return
    amt3 = round(amount, 3)
    live = [p for p in _pending_crypto_orders() if p.get("network") == network]
    # 1) exact unique-amount match (99.00X)
    for p in live:
        try: want = round(float(p["amount"]), 3)
        except Exception: continue
        if abs(want - amt3) < 0.0005:
            _fulfill_payment(p["order_id"], p.get("email", ""), amount); _set_txid(p["order_id"], txid)
            print("crypto: matched %s %.3f -> %s (%s) tx %s" % (network, amount, p["order_id"], p.get("email",""), txid[:16]))
            return
    # 2) fallback: buyer rounded (e.g. sent 99.00 for a 99.007 order). If exactly ONE live
    #    order on this network is within 1 USDT, it's unambiguous -> match it.
    near = [p for p in live if abs(float(p.get("amount") or 0) - amount) < 1.0]
    if len(near) == 1:
        p = near[0]
        _fulfill_payment(p["order_id"], p.get("email", ""), amount); _set_txid(p["order_id"], txid)
        print("crypto: fallback-matched %s %.3f -> %s (%s) tx %s" % (network, amount, p["order_id"], p.get("email",""), txid[:16]))
        return
    # 3) ambiguous or no order -> record for one-click manual grant (stays retryable)
    if amount >= PRICE_USDT * 0.9:
        uid = "U-" + txid[:20]
        if not _get_payment(uid):
            _upsert_payment(uid, "", amount, "unmatched", False)
            _set_txid(uid, txid)
            print("crypto: UNMATCHED %s %.3f tx %s (needs manual grant)" % (network, amount, txid[:16]))

def _poll_tron():
    url = ("https://api.trongrid.io/v1/accounts/%s/transactions/trc20"
           "?only_confirmed=true&limit=40&order_by=block_timestamp,desc&contract_address=%s"
           % (USDT_TRON_ADDR, USDT_TRC20_CONTRACT))
    headers = {"TRON-PRO-API-KEY": TRONGRID_KEY} if TRONGRID_KEY else {}
    data = _http_get_json(url, headers)
    for t in (data.get("data") or []):
        if (t.get("to") or "") != USDT_TRON_ADDR: continue
        try: amt = int(t.get("value") or 0) / 1e6
        except Exception: continue
        _match_incoming("tron", amt, t.get("transaction_id") or "")

def _poll_erc20():
    url = ("https://api.etherscan.io/v2/api?chainid=1&module=account&action=tokentx"
           "&contractaddress=%s&address=%s&page=1&offset=40&sort=desc&apikey=%s"
           % (USDT_ERC20_CONTRACT, USDT_ERC20_ADDR, ETHERSCAN_KEY))
    data = _http_get_json(url)
    if str(data.get("status")) != "1": return
    for t in (data.get("result") or []):
        if (t.get("to") or "").lower() != USDT_ERC20_ADDR.lower(): continue
        try:
            dec = int(t.get("tokenDecimal") or 6)
            amt = int(t.get("value") or 0) / (10 ** dec)
        except Exception: continue
        _match_incoming("erc20", amt, t.get("hash") or "")

def _poll_crypto_once():
    if CRYPTO_TRON_ON:
        try: _poll_tron()
        except Exception as e: print("tron poll err:", e)
    if CRYPTO_ERC20_ON:
        try: _poll_erc20()
        except Exception as e: print("erc20 poll err:", e)

def _crypto_watcher_loop():
    print("crypto watcher started (tron=%s erc20=%s)" % (CRYPTO_TRON_ON, CRYPTO_ERC20_ON))
    while True:
        try: _poll_crypto_once()
        except Exception as e: print("crypto watcher err:", e)
        time.sleep(30)

def _verify_stripe_sig(payload: bytes, sig_header: str):
    """Verify a Stripe webhook signature (Stripe-Signature: t=...,v1=...)."""
    if not STRIPE_WEBHOOK_SECRET or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return False
    signed = t.encode() + b"." + payload
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    return secrets.compare_digest(expected, v1)

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

def _delete_member(email):
    """Permanently remove a member and everything linked to them (referrals, payments, live sessions)."""
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("DELETE FROM members WHERE email=%s", (email,))
        cur.execute("DELETE FROM commissions WHERE referred=%s OR referrer=%s", (email, email))
        cur.execute("DELETE FROM payments WHERE email=%s", (email,))
        conn.commit(); cur.close(); conn.close()
    else:
        m = _load_file()
        if email in m:
            del m[email]; _save_file(m)
    # kill any live login sessions belonging to this member
    for tok in [t for t, v in list(_SESSIONS.items()) if (v[0] if isinstance(v, (tuple, list)) else v) == email]:
        _SESSIONS.pop(tok, None)

def _hash(pw, salt): return hashlib.sha256((salt + pw).encode()).hexdigest()

# ---------- global key/value settings (risk banner, etc.) ----------
SETTINGS_FILE = os.path.join(SITE, "settings.json")
def _get_setting(key, default=None):
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone(); cur.close(); conn.close()
        if not row: return default
        val = row["value"] if isinstance(row, dict) else row[0]
    else:
        try: val = json.load(open(SETTINGS_FILE, encoding="utf-8")).get(key)
        except Exception: val = None
        if val is None: return default
    try: return json.loads(val)
    except Exception: return val
def _set_setting(key, value):
    val = json.dumps(value)
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("""INSERT INTO settings(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""", (key, val))
        conn.commit(); cur.close(); conn.close()
    else:
        try: data = json.load(open(SETTINGS_FILE, encoding="utf-8"))
        except Exception: data = {}
        data[key] = val
        json.dump(data, open(SETTINGS_FILE, "w", encoding="utf-8"))

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
    if SELFCRYPTO_ENABLED:
        try: threading.Thread(target=_crypto_watcher_loop, daemon=True).start()
        except Exception as e: print("crypto watcher start note:", e)

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
    if not country or country.strip().lower() == "other":
        # "Other" used to be the only option that fit non-Balkan members, so 73% of the base
        # landed there and real geography was invisible. The dropdown now lists every country;
        # reject the empty pick and the legacy "Other" so a stale cached page can't reintroduce it.
        return JSONResponse({"ok": False, "error": "Please select your country"})
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
    # affiliate: give this member their own referral code, and attribute the referrer if valid
    _set_member_field(email, "ref_code", _uniq_ref_code())
    ref = (body.get("ref") or "").strip().upper()[:12]
    if ref and _member_by_ref(ref):
        _set_member_field(email, "referred_by", ref)
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

@app.get("/api/telegram/invite")
def telegram_invite(k_session: str = Cookie(None)):
    """Private signals-channel invite link. Paid/trial members only — the link is
    never sent to non-subscribers, so it can't leak to free users via the page source."""
    email = _session_email(k_session)
    if not email: return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, label, _ = _access_state(u)
    # PAID members only — trial/free/expired do not get the private signals channel
    if label != "active" or not TELEGRAM_INVITE_LINK:
        return JSONResponse({"ok": False, "error": label}, status_code=403)
    return {"ok": True, "url": TELEGRAM_INVITE_LINK}

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
    candles = body.get("candles") or []
    if not isinstance(candles, list): candles = []
    open_trades = body.get("open_trades") or []
    if not isinstance(open_trades, list): open_trades = []

    # ---- the track record is APPEND-ONLY ----
    # This endpoint used to overwrite the published history with whatever the PC sent. On
    # 2026-07-14 a crash wiped the PC's state file, it pushed an empty history, and the entire
    # published record was erased in one 60s cycle. The record may now only grow: a push that
    # omits history, or carries fewer trades than we already hold, leaves the stored record
    # untouched. A deliberate correction must be explicit (history_replace: true).
    try:
        stored = json.load(open(SIGNALS_FILE, encoding="utf-8")) if os.path.exists(SIGNALS_FILE) else {}
    except Exception:
        stored = {}
    kept = stored.get("history") or []
    if not isinstance(kept, list): kept = []

    hist = body.get("history")
    replace = bool(body.get("history_replace"))
    if not isinstance(hist, list):
        hist, note = kept, "history omitted by pusher; kept stored record"
    elif len(hist) < len(kept) and not replace:
        note = f"REFUSED history shrink {len(kept)} -> {len(hist)}; kept stored record"
        hist = kept
    else:
        note = None

    safe = {
        "generated_utc": body.get("generated_utc"),
        "price": body.get("price"),
        "trend": body.get("trend"),
        "news_status": body.get("news_status"),
        "next_event": body.get("next_event"),
        "signals": body.get("signals") or [],
        "history": hist[-5000:],
        "open_trades": open_trades[:8],
        "candles": candles[-400:],
    }
    try:
        json.dump(safe, open(SIGNALS_FILE, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    out = {"ok": True, "count": len(safe["signals"]), "history": len(safe["history"])}
    if note:
        out["warning"] = note
    return out

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

# NOTE: /api/ingest_chart and /api/chart.png were REMOVED on purpose.
# The pushed PNG was a raw TradingView screenshot, so it baked the EMA labels and the
# "zones ride EMA 34/89/130/200 - SL 10 - TP 5/10/20+" footer into an image that any
# logged-in member could fetch. The members page now renders its own chart from the
# anonymised `candles` + `signals` payload instead. Do not reintroduce these routes.

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
        return {"ok": True, "email": email, "msg": "3-day trial started"}
    elif action == "extend_month":
        _grant_access(email, MONTH_DAYS, "paid")
        _accrue_referral(email)
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
        if plan == "paid":
            _accrue_referral(email)
        return {"ok": True, "email": email, "msg": f"+{days} days ({plan})",
                "until": time.strftime("%Y-%m-%d", time.gmtime(nu))}
    elif action == "set_expiry":
        plan = (body.get("plan") or "paid").strip().lower()
        ts = _set_expiry(email, (body.get("date") or "").strip(), plan)
        if ts is None:
            return JSONResponse({"ok": False, "error": "bad date (use YYYY-MM-DD)"})
        if plan == "paid":
            _accrue_referral(email)
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
    elif action == "delete":
        rec = _get_member(email)
        if _member_is_admin(rec) and not body.get("force"):
            return JSONResponse({"ok": False, "error": "This is an admin account - re-confirm to delete it."})
        _delete_member(email)
        return {"ok": True, "email": email, "msg": "member permanently deleted", "deleted": True}
    return JSONResponse({"ok": False, "error": "bad action"})

RISK_LEVELS = {"elevated", "high", "very_high"}
@app.get("/api/risk_alert")
def risk_alert():
    """Current members-area risk banner (public - it is a safety notice, not secret)."""
    a = _get_setting("risk_alert") or {}
    if not a.get("enabled"):
        return {"enabled": False}
    return {"enabled": True, "level": a.get("level", "high"),
            "title": a.get("title", ""), "message": a.get("message", ""),
            "updated": a.get("updated", "")}

@app.post("/api/admin/risk_alert")
async def admin_risk_alert(request: Request, k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    body = await request.json()
    enabled = bool(body.get("enabled"))
    level = (body.get("level") or "high").strip().lower()
    if level not in RISK_LEVELS: level = "high"
    alert = {"enabled": enabled, "level": level,
             "title": (body.get("title") or "").strip()[:120],
             "message": (body.get("message") or "").strip()[:600],
             "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    _set_setting("risk_alert", alert)
    return {"ok": True, "alert": alert}

SITE_VERSION = "day-picker-academy-1"   # bump on notable deploys; check at /api/version

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
        "trades": list(reversed(trades))[:1000],
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    }

# ==================== US30 (DJ30) — isolated mirror of the gold pipeline ====================
# Separate state files and separate /api/us30/* endpoints. The gold endpoints and files
# above are never read or written here. Same auth/access gating, same INGEST_TOKEN.
SIGNALS_FILE_US30 = os.path.join(SITE, "live_signals_us30.json")
TICK_FILE_US30 = os.path.join(SITE, "tick_us30.json")

@app.post("/api/us30/ingest_signals")
async def ingest_signals_us30(request: Request):
    if not INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "ingest disabled"}, status_code=403)
    if request.headers.get("x-ingest-token", "") != INGEST_TOKEN:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if body.get("engine") != "nypipeline":
        return JSONResponse({"ok": False, "error": "legacy payload ignored"}, status_code=409)
    hist = body.get("history") or []
    if not isinstance(hist, list): hist = []
    candles = body.get("candles") or []
    if not isinstance(candles, list): candles = []
    open_trades = body.get("open_trades") or []
    if not isinstance(open_trades, list): open_trades = []
    safe = {
        "generated_utc": body.get("generated_utc"), "price": body.get("price"),
        "trend": body.get("trend"), "news_status": body.get("news_status"),
        "next_event": body.get("next_event"), "signals": body.get("signals") or [],
        "history": hist[-5000:], "open_trades": open_trades[:8], "candles": candles[-400:],
    }
    try:
        json.dump(safe, open(SIGNALS_FILE_US30, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "count": len(safe["signals"])}

@app.get("/api/us30/signals")
def signals_us30(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email: return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, label, days_left = _access_state(u)
    if not has_access: return JSONResponse({"error": label}, status_code=403)
    if not os.path.exists(SIGNALS_FILE_US30):
        return {"signals": [], "price": None, "trend": None, "generated_utc": None, "stale": True}
    try:
        data = json.load(open(SIGNALS_FILE_US30, encoding="utf-8"))
    except Exception:
        return {"signals": [], "stale": True}
    try:
        age = time.time() - os.path.getmtime(SIGNALS_FILE_US30)
        data["stale"] = age > 1200
        data["age_seconds"] = int(age)
    except Exception:
        data["stale"] = False
    return data

@app.post("/api/us30/ingest_tick")
async def ingest_tick_us30(request: Request):
    if not INGEST_TOKEN: return JSONResponse({"ok": False}, status_code=403)
    if request.headers.get("x-ingest-token", "") != INGEST_TOKEN:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        body = await request.json(); price = float(body.get("price"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    try:
        with open(TICK_FILE_US30, "w") as f:
            json.dump({"price": price, "t": time.time()}, f)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True}

@app.get("/api/us30/tick")
def tick_us30(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email: return JSONResponse({"error": "members only"}, status_code=401)
    u = _get_member(email) or {}
    has_access, _, _ = _access_state(u)
    if not has_access: return JSONResponse({"error": "no access"}, status_code=403)
    try:
        d = json.load(open(TICK_FILE_US30))
        return {"price": d.get("price"), "age": round(time.time() - d.get("t", 0), 1)}
    except Exception:
        return {"price": None, "age": None}

# /api/us30/ingest_chart and /api/us30/chart.png removed for the same reason as the gold
# routes: the pushed screenshot leaked the EMA labels + strategy footer. See note above.

@app.get("/api/us30/public_stats")
def public_stats_us30():
    hist = []
    if os.path.exists(SIGNALS_FILE_US30):
        try:
            hist = (json.load(open(SIGNALS_FILE_US30, encoding="utf-8")) or {}).get("history") or []
        except Exception:
            hist = []
    trades, pips_total, wins, sl_count, tp3_count = [], 0.0, 0, 0, 0
    for r in hist:
        o = r.get("outcome"); pts = _trade_points(r)
        pips_total += pts * 10
        if o == "SL": sl_count += 1
        else:
            wins += 1
            if o == "TP3": tp3_count += 1
        cu = (r.get("closed_utc") or "")
        exit_price = r.get("tp3") if o == "TP3" else r.get("sl")
        trades.append({"date": cu[:10], "time": cu[11:16], "side": r.get("side", ""),
            "entry": r.get("entry", ""), "exit": exit_price if exit_price is not None else "",
            "pips": round(pts * 10), "result": "LOSS" if o == "SL" else "WIN", "outcome": o})
    total = len(trades)
    return {"total": total, "wins": wins,
        "win_rate": round(wins * 100 / total) if total else 0,
        "sl_count": sl_count, "tp3_count": tp3_count,
        "pips_total": round(pips_total), "usd_1lot": round(pips_total * 10),
        "trades": list(reversed(trades))[:1000],
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}

@app.get("/us30")
def us30_page(k_session: str = Cookie(None)):
    if not _session_email(k_session): return RedirectResponse("/login")
    return _page("members_us30.html")
# ==================== end US30 mirror ====================

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

# ==================== Kasandra AI assistant ====================
KASANDRA_KB = """You are the Kasandra Assistant, the AI support agent for Kasandra (kasandra.app),
an automated gold (XAU/USD) trading-signal service. You answer visitors and members warmly, briefly,
and accurately. Match the user's language (English, Macedonian, or Serbian).

WHAT KASANDRA IS
- A rules-based automated system that trades gold (XAU/USD) and publishes every signal live: entry zone, stop-loss, and three take-profit targets (TP1/TP2/TP3).
- Members trade the signals in their OWN broker account. Kasandra never touches or holds anyone's money. Funds stay with the broker in the member's name.
- Membership is 99 USDT per month. There is a 3-day free trial.

HOW IT WORKS
- The system watches gold around the clock and arms "zones" at key levels. When price reaches a zone it becomes a live signal.
- Each signal: enter in the zone, stop-loss ~100 pips, targets TP1 (+50 pips), TP2 (+100), TP3 (+200). Suggested management: move stop to breakeven after TP1.
- Signals go to the members site (live chart + history), and paid members also get an instant private Telegram channel with get-ready -> firm entry -> TP/SL updates.
- Two ways to trade the signals: (1) MANUAL - you place each trade yourself in your own account, in your own time (the default, works with any supported broker); or (2) COPY TRADING - fully automatic, the trades are placed for you.

COPY TRADING (IMPORTANT - Kasandra DOES offer this; never turn a copy-trade request away)
- If anyone asks about copy trading / automatic trading / auto-copy / hands-off / "does it trade for me": the answer is YES. Confirm it warmly and explain the two requirements.
- It is available ONLY to (1) PAID members who are (2) using a VT Markets account opened through Kasandra's VT Markets link. Copy trading runs on VT Markets only, not the other brokers.
- Even with copy trading, the money stays in the member's own VT Markets account, in their name - Kasandra never holds or withdraws funds; it only places the signal trades automatically.
- To get set up, the member contacts @Kasandra_tech on Telegram and the team connects their account. Do NOT tell a copy-trade seeker to go research other platforms - Kasandra has it.

THE RECORD (be precise, never inflate)
- The public track record is broker-verified: all 165 signals in the June-July 2026 window were replayed against the broker's own price bars, and the record was found CONSERVATIVE (it under-claimed — published 28 losses where broker data showed only 11). A downloadable audit report exists.
- Past performance never guarantees future results. Trading gold/CFDs is high risk; only risk capital you can afford to lose.

BROKERS
- Members open an account with a partner broker (VT Markets is the main one; QUO Markets, PU Prime, and TradeQuo also supported). There are invite links and a "connect" step on the site.

PAYMENTS
- Card via Stripe, or crypto (USDT). Access activates automatically on payment. Manual crypto payment is possible via @Kasandra_tech on Telegram.

CONTACT / HUMAN HANDOFF
- For anything you cannot answer, account/payment issues, or if the user asks for a human: point them to Telegram @Kasandra_tech, or this live chat where the team also replies.

STRICT RULES
- NEVER give personalized financial or investment advice, position sizing for someone's specific account, or predictions ("gold will go up"). If asked, say you can't advise and point to the Risk Management page.
- NEVER reveal the internal strategy logic, the exact indicator, EMA settings, or any proprietary detail. It's a rules-based system; that's all you disclose.
- Don't invent numbers, promotions, or guarantees. If you don't know, say so and offer the human contact.
- Keep replies short (2-4 sentences). Be encouraging but honest about risk."""

CHAT_LOG_FILE = os.path.join(SITE, "chat_logs.json")

def _log_chat(session, ip, role, content):
    created = time.strftime("%Y-%m-%d %H:%M:%S")
    row = {"session": session, "ip": ip, "role": role, "content": content[:4000], "created": created}
    try:
        if _USE_DB:
            conn = _db(); cur = conn.cursor()
            cur.execute("INSERT INTO chat_logs(session,ip,role,content,created) VALUES(%s,%s,%s,%s,%s)",
                        (session, ip, role, row["content"], created))
            conn.commit(); cur.close(); conn.close()
        else:
            data = []
            if os.path.exists(CHAT_LOG_FILE):
                try: data = json.load(open(CHAT_LOG_FILE, encoding="utf-8"))
                except Exception: data = []
            data.append(row); data = data[-5000:]
            json.dump(data, open(CHAT_LOG_FILE, "w", encoding="utf-8"))
    except Exception as e:
        print("chat log error:", e)

@app.post("/api/chat")
async def chat(request: Request):
    if not CHAT_ENABLED:
        return JSONResponse({"ok": False, "error": "assistant offline"}, status_code=503)
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
    now = time.time()
    hits = [t for t in _chat_rate.get(ip, []) if now - t < 60]
    if len(hits) >= 12:
        return JSONResponse({"ok": False, "error": "Slow down a moment and try again."}, status_code=429)
    hits.append(now); _chat_rate[ip] = hits
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    history = body.get("messages") or []
    session = (str(body.get("session_id") or "")[:40]) or ("ip-" + str(abs(hash(ip)) % 10**8))
    msgs = []
    for m in history[-10:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "")[:2000]
        if text.strip():
            msgs.append({"role": role, "content": text})
    if not msgs or msgs[-1]["role"] != "user":
        return JSONResponse({"ok": False, "error": "no message"}, status_code=400)
    _log_chat(session, ip, "user", msgs[-1]["content"])
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=500,
            system=[{"type": "text", "text": KASANDRA_KB, "cache_control": {"type": "ephemeral"}}],
            messages=msgs,
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if not reply:
            reply = "Sorry, could you rephrase that? For anything urgent you can reach the team on Telegram @Kasandra_tech."
        _log_chat(session, ip, "assistant", reply)
        return {"ok": True, "reply": reply}
    except Exception as e:
        print("chat error:", e)
        return JSONResponse({"ok": False, "error": "The assistant is busy right now — please message @Kasandra_tech on Telegram."}, status_code=502)

@app.get("/api/chat/status")
def chat_status():
    return {"enabled": CHAT_ENABLED}

@app.get("/api/admin/chats")
def admin_chats(k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    rows = []
    if _USE_DB:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT session,ip,role,content,created FROM chat_logs ORDER BY id DESC LIMIT 2000")
        rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
        rows.reverse()
    elif os.path.exists(CHAT_LOG_FILE):
        try: rows = json.load(open(CHAT_LOG_FILE, encoding="utf-8"))[-2000:]
        except Exception: rows = []
    convos = {}
    for r in rows:
        s = r.get("session", "?")
        c = convos.setdefault(s, {"session": s, "ip": r.get("ip", ""), "last": r.get("created", ""), "messages": []})
        c["messages"].append({"role": r.get("role"), "content": r.get("content"), "created": r.get("created")})
        c["last"] = r.get("created", c["last"])
    out = sorted(convos.values(), key=lambda c: c["last"], reverse=True)
    return {"conversations": out, "count": len(out), "messages": len(rows)}

@app.get("/chatlog")
def chatlog_page(): return _page("chatlog.html")

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

def _mask_email(e):
    if not e or "@" not in e:
        return e
    u, d = e.split("@", 1)
    return (u[0] + "***" if len(u) > 1 else "***") + "@" + d

@app.get("/api/affiliate/me")
def affiliate_me(k_session: str = Cookie(None)):
    """A member's own affiliate stats: their code/link, referrals and earnings."""
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"error": "members only"}, status_code=401)
    code = _ensure_ref_code(email)
    refs = []
    for e, m in _all_members().items():
        if m.get("referred_by") == code:
            _, label, _dl = _access_state(m)
            refs.append({"name": (m.get("name") or e), "email": _mask_email(e),
                         "status": label, "joined": m.get("joined", "")})
    refs.sort(key=lambda r: r.get("joined", ""), reverse=True)
    comms = [c for c in _load_commissions() if c.get("referrer") == email]
    pending = round(sum(c["amount"] for c in comms if c.get("status") == "pending"))
    settled = round(sum(c["amount"] for c in comms if c.get("status") == "settled"))
    return {"ref_code": code, "pct": REF_PCT, "per_month": REF_COMMISSION,
            "referrals": refs, "signups": len(refs),
            "paid": sum(1 for r in refs if r["status"] == "active"),
            "earned_pending": pending, "earned_settled": settled, "earned_total": pending + settled}

@app.get("/api/admin/affiliates")
def admin_affiliates(k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    members = _all_members()
    comms = _load_commissions()
    by_code = {}
    for e, m in members.items():
        if m.get("referred_by"):
            by_code.setdefault(m["referred_by"], []).append((e, m))
    earners = set(c["referrer"] for c in comms)
    for e, m in members.items():
        if m.get("ref_code") and by_code.get(m["ref_code"]):
            earners.add(e)
    rows = []
    for e in earners:
        m = members.get(e)
        if not m:
            continue
        my_refs = by_code.get(m.get("ref_code"), [])
        my_comms = [c for c in comms if c.get("referrer") == e]
        pending = round(sum(c["amount"] for c in my_comms if c.get("status") == "pending"))
        settled = round(sum(c["amount"] for c in my_comms if c.get("status") == "settled"))
        rows.append({"email": e, "name": m.get("name") or e, "ref_code": m.get("ref_code"),
                     "signups": len(my_refs),
                     "paid": sum(1 for (_re, rm) in my_refs if _access_state(rm)[1] == "active"),
                     "owed": pending, "settled": settled})
    rows.sort(key=lambda r: (-r["owed"], -r["signups"]))
    return {"affiliates": rows, "total_owed": round(sum(r["owed"] for r in rows)),
            "pct": REF_PCT, "per_month": REF_COMMISSION}

@app.post("/api/admin/affiliate/settle")
async def admin_affiliate_settle(request: Request, k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    n = _settle_commissions(email)
    return {"ok": True, "settled": n, "msg": f"marked {n} commission(s) as paid out"}

@app.get("/api/pay/status")
def pay_status():
    # only advertise a method that will actually auto-activate the member on payment
    return {"enabled": PAY_ENABLED or STRIPE_AUTO or SELFCRYPTO_ENABLED,
            "crypto": SELFCRYPTO_ENABLED or PAY_ENABLED,
            "selfcrypto": SELFCRYPTO_ENABLED, "tron": CRYPTO_TRON_ON, "erc20": CRYPTO_ERC20_ON,
            "stripe": STRIPE_AUTO, "price": PRICE_USDT}

@app.post("/api/pay/crypto/create")
async def crypto_create(request: Request, k_session: str = Cookie(None)):
    """Create a pending USDT order with a unique amount; the watcher auto-grants on arrival."""
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"ok": False, "error": "members only"}, status_code=401)
    try: body = await request.json()
    except Exception: body = {}
    network = (body.get("network") or "tron").lower()
    if network in ("erc20", "eth", "ethereum"):
        network, addr, on = "erc20", USDT_ERC20_ADDR, CRYPTO_ERC20_ON
    else:
        network, addr, on = "tron", USDT_TRON_ADDR, CRYPTO_TRON_ON
    if not on or not addr:
        return JSONResponse({"ok": False, "error": "that network isn't available"}, status_code=503)
    amount = _unique_crypto_amount(network)
    order_id = "C-" + secrets.token_hex(8)
    _create_crypto_order(order_id, email, amount, network)
    return {"ok": True, "order_id": order_id, "network": network, "address": addr,
            "amount": amount, "asset": "USDT", "price": PRICE_USDT, "expires_in": ORDER_LIFETIME,
            "chain": "TRON (TRC20)" if network == "tron" else "Ethereum (ERC20)"}

@app.get("/api/pay/crypto/status")
def crypto_status(order_id: str = "", k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"ok": False, "error": "members only"}, status_code=401)
    p = _get_payment(order_id) or {}
    if not p or p.get("email") != email:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, "status": p.get("status"), "paid": bool(p.get("fulfilled"))}

@app.post("/api/pay/create")
async def pay_create(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"ok": False, "error": "members only"}, status_code=401)
    if not PAY_ENABLED:
        return JSONResponse({"ok": False, "error": "payments not configured yet"}, status_code=503)
    order_id = "K-" + secrets.token_hex(8)
    _upsert_payment(order_id, email, PRICE_USDT, "pending", False)
    try:
        res = _cryptomus_create_invoice(order_id, PRICE_USDT, email)
        url = (res.get("result") or {}).get("url")
        if not url:
            raise ValueError("no checkout url: " + json.dumps(res)[:200])
        return {"ok": True, "url": url}
    except Exception as e:
        return JSONResponse({"ok": False, "error": "could not create checkout"}, status_code=502)

@app.post("/api/pay/webhook")
async def pay_webhook(request: Request):
    """Cryptomus payment callback. Signature-verified; grants access on a real 'paid' event."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    if not PAY_ENABLED or not _verify_webhook(data):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=403)
    order_id = data.get("order_id") or ""
    status = (data.get("status") or "").lower()
    try:
        amount = float(data.get("amount") or data.get("payment_amount") or 0)
    except Exception:
        amount = 0.0
    stored = _get_payment(order_id) or {}
    email = (stored.get("email") or data.get("additional_data") or "").strip().lower()
    if status in ("paid", "paid_over") and email and amount >= PRICE_USDT * 0.98:
        _fulfill_payment(order_id, email, amount)
    else:
        _upsert_payment(order_id, email, amount, status or "unknown", bool(stored.get("fulfilled")))
    return {"ok": True}

@app.post("/api/pay/stripe")
async def pay_stripe(k_session: str = Cookie(None)):
    email = _session_email(k_session)
    if not email:
        return JSONResponse({"ok": False, "error": "members only"}, status_code=401)
    if not STRIPE_ENABLED:
        return JSONResponse({"ok": False, "error": "card payments not configured"}, status_code=503)
    order_id = "S-" + secrets.token_hex(8)
    _upsert_payment(order_id, email, PRICE_USDT, "pending", False)
    sep = "&" if "?" in STRIPE_PAYMENT_LINK else "?"
    return {"ok": True, "url": STRIPE_PAYMENT_LINK + sep + "client_reference_id=" + order_id}

@app.post("/api/pay/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe checkout webhook. Signature-verified; grants access on a paid session."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not STRIPE_AUTO or not _verify_stripe_sig(payload, sig):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=403)
    try:
        event = json.loads(payload)
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    if event.get("type") == "checkout.session.completed":
        s = (event.get("data") or {}).get("object") or {}
        if (s.get("payment_status") or "") == "paid":
            order_id = s.get("client_reference_id") or ("S-" + secrets.token_hex(6))
            amount = round(float(s.get("amount_total") or 0) / 100, 2) or PRICE_USDT
            stored = _get_payment(order_id) or {}
            email = (stored.get("email") or (s.get("customer_details") or {}).get("email") or "").strip().lower()
            if email:
                _fulfill_payment(order_id, email, amount)
    return {"ok": True}

@app.get("/api/admin/payments")
def admin_payments(k_admin: str = Cookie(None)):
    if not _is_admin(k_admin):
        return JSONResponse({"error": "admin only"}, status_code=401)
    ps = _load_payments()[:150]
    paid = [p for p in ps if (p.get("status") or "") in ("paid", "paid_over")]
    revenue = round(sum(float(p.get("amount") or 0) for p in paid))
    return {"payments": ps, "count": len(ps), "paid_count": len(paid),
            "revenue": revenue, "enabled": PAY_ENABLED or STRIPE_AUTO,
            "crypto": PAY_ENABLED, "stripe": STRIPE_AUTO}

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

@app.get("/academy")
def academy_page(k_session: str = Cookie(None)):   # members-only (registered users)
    if not _session_email(k_session): return RedirectResponse("/login")
    return _page("academy.html")

@app.get("/admin")
def admin_page(): return _page("admin.html")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
