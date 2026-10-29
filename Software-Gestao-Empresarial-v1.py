# FILE: alphatech_business_hybrid/.env.example
APP_SECRET_KEY=REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_32_CHARS
DATABASE_PATH=./business_hybrid.db
APP_MODE=development
# Em produção: APP_MODE=production e URL pública HTTPS com proxy TLS configurado.
 
# FILE: alphatech_business_hybrid/.gitignore
.venv/
__pycache__/
.pytest_cache/
*.py[cod]
*.db
.env
 
# FILE: alphatech_business_hybrid/app/__init__.py
 
# FILE: alphatech_business_hybrid/app/db.py
import os
import sqlite3
from pathlib import Path
 
DB_PATH = os.getenv("DATABASE_PATH", "./business_hybrid.db")
 
 
def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn
 
 
def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, activation_code_hash TEXT,
            activation_expires_at TEXT, activation_used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT NOT NULL, company_id TEXT NOT NULL, kind TEXT NOT NULL,
            category TEXT NOT NULL, description TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            occurred_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'online',
            synced INTEGER NOT NULL DEFAULT 1, created_by TEXT, device_id TEXT,
            PRIMARY KEY(company_id, id), FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_company_date ON transactions(company_id, occurred_at);
        CREATE TABLE IF NOT EXISTS activation_devices (
            device_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, token_hash TEXT NOT NULL,
            activated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TEXT,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, actor TEXT NOT NULL,
            action TEXT NOT NULL, object_id TEXT, detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE TABLE IF NOT EXISTS auth_attempts (
            key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_start REAL NOT NULL
        );
        """)
        # Migrate databases created by the previous MVP version.
        company_cols = {r["name"] for r in db.execute("PRAGMA table_info(companies)").fetchall()}
        if "activation_code" in company_cols and "activation_code_hash" not in company_cols:
            db.execute("ALTER TABLE companies ADD COLUMN activation_code_hash TEXT")
            db.execute("ALTER TABLE companies ADD COLUMN activation_expires_at TEXT")
            db.execute("ALTER TABLE companies ADD COLUMN activation_used_at TEXT")
            # Legacy plaintext codes are intentionally invalidated; keep only a non-secret tombstone
            # because old schemas declared activation_code NOT NULL.
            db.execute("UPDATE companies SET activation_code='INVALIDATED-' || id, activation_code_hash=NULL, activation_expires_at=NULL, activation_used_at=NULL")
        elif "activation_code" in company_cols:
            # Finish migration safely if a previous deployment added the hash columns but retained plaintext codes.
            db.execute("UPDATE companies SET activation_code='INVALIDATED-' || id WHERE activation_code_hash IS NULL OR activation_code_hash=''")
        tx_cols = {r["name"] for r in db.execute("PRAGMA table_info(transactions)").fetchall()}
        pk_cols = [r["name"] for r in db.execute("PRAGMA table_info(transactions)").fetchall() if r["pk"]]
        if pk_cols == ["id"]:
            db.execute("ALTER TABLE transactions RENAME TO transactions_legacy")
            db.executescript("""
            CREATE TABLE transactions (
                id TEXT NOT NULL, company_id TEXT NOT NULL, kind TEXT NOT NULL,
                category TEXT NOT NULL, description TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                occurred_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'online', synced INTEGER NOT NULL DEFAULT 1,
                created_by TEXT, device_id TEXT, PRIMARY KEY(company_id,id), FOREIGN KEY(company_id) REFERENCES companies(id)
            );
            INSERT OR IGNORE INTO transactions(id,company_id,kind,category,description,amount_cents,occurred_at,source,synced)
              SELECT id,company_id,kind,category,description,amount_cents,occurred_at,source,synced FROM transactions_legacy;
            DROP TABLE transactions_legacy;
            CREATE INDEX IF NOT EXISTS idx_transactions_company_date ON transactions(company_id, occurred_at);
            """)
        device_cols = {r["name"] for r in db.execute("PRAGMA table_info(activation_devices)").fetchall()}
        if "token_hash" not in device_cols:
            db.execute("ALTER TABLE activation_devices ADD COLUMN token_hash TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE activation_devices ADD COLUMN revoked_at TEXT")
            db.execute("UPDATE activation_devices SET revoked_at=CURRENT_TIMESTAMP WHERE token_hash='' ")
 
# FILE: alphatech_business_hybrid/app/main.py
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from uuid import UUID, uuid4
import hashlib
import hmac
import json
import os
import secrets
import time
 
# Load .env explicitly without overriding real environment variables.
ROOT_DIR = Path(__file__).resolve().parent.parent
_env_file = ROOT_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
 
from fastapi import FastAPI, Form, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
 
from .db import connect, init_db
from .security import hash_password, verify_password, make_activation_code, secret_key, hash_secret, csrf_token, normalize_timestamp, utc_now_iso
 
BASE_DIR = Path(__file__).resolve().parent
 
def _is_production():
    return os.getenv("APP_MODE", "development").lower() == "production"
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Force secret validation at startup, not after serving requests.
    secret_key()
    init_db()
    yield
 
app = FastAPI(title="AlphaTech-JLC | Gestão Empresarial Híbrida", version="0.2.0", lifespan=lifespan,
              docs_url=None if _is_production() else "/docs", redoc_url=None if _is_production() else "/redoc",
              openapi_url=None if _is_production() else "/openapi.json")
app.add_middleware(SessionMiddleware, secret_key=secret_key(), same_site="lax", https_only=_is_production(), max_age=60*60*8)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
 
# Simple process-local limiter for the MVP. Production should use a shared Redis limiter.
_RATE: dict[str, list[float]] = {}
def rate_limit(key: str, limit: int = 8, window: int = 300):
    now = time.time()
    entries = [t for t in _RATE.get(key, []) if now - t < window]
    if len(entries) >= limit:
        raise HTTPException(429, "Demasiadas tentativas. Aguarde antes de tentar novamente.")
    entries.append(now); _RATE[key] = entries
 
def client_key(request: Request, prefix: str):
    return prefix + ":" + (request.client.host if request.client else "unknown")
 
def csrf_for(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = csrf_token(); request.session["csrf_token"] = token
    return token
 
def verify_csrf(request: Request, supplied: str):
    expected = request.session.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "Token CSRF inválido. Atualize a página e tente novamente.")
 
def current_company(request: Request):
    company_id = request.session.get("company_id")
    if not company_id: return None
    with connect() as db:
        return db.execute("SELECT id, name, username FROM companies WHERE id=?", (company_id,)).fetchone()
 
def require_company(request: Request):
    company = current_company(request)
    if not company: raise HTTPException(status_code=401, detail="Sessão não autenticada")
    return company
 
def audit(db, company_id, actor, action, object_id=None, detail=""):
    db.execute("INSERT INTO audit_log(company_id,actor,action,object_id,detail,created_at) VALUES(?,?,?,?,?,?)",
               (company_id, actor, action, object_id, detail[:500], utc_now_iso()))
 
def validate_amount(value: str) -> int:
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("valor inválido")
    normalized = value.strip().replace(" ", "")
    if "," in normalized and "." in normalized:
        raise ValueError("use apenas um separador decimal; exemplo: 1234,56")
    normalized = normalized.replace(",", ".")
    try: amount = Decimal(normalized)
    except InvalidOperation as exc: raise ValueError("valor inválido") from exc
    if not amount.is_finite(): raise ValueError("valor deve ser finito")
    if amount <= 0 or amount > Decimal(10**10): raise ValueError("o valor deve ser positivo e válido")
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("valor fora do intervalo permitido") from exc
    cents_int = int(cents)
    if cents_int <= 0 or cents_int > 10**12: raise ValueError("o valor deve ser positivo e válido")
    return cents_int
 
def secure_base_url(url: str):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https" and parsed.hostname: return url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in local_hosts and not _is_production(): return url.rstrip("/")
    raise HTTPException(400, "A ativação exige HTTPS; HTTP só é permitido para localhost em desenvolvimento.")
 
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if _is_production(): response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response
 
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if current_company(request): return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None, "csrf_token": csrf_for(request)})
 
@app.get("/registo", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html", context={"error": None, "csrf_token": csrf_for(request)})
 
@app.post("/registo", response_class=HTMLResponse)
def register(request: Request, csrf_token_form: str = Form(..., alias="csrf_token"), company_name: str = Form(...), username: str = Form(...), password: str = Form(...)):
    verify_csrf(request, csrf_token_form); rate_limit(client_key(request, "register"), 4, 3600)
    company_name, username = company_name.strip(), username.strip().lower()
    if len(company_name) < 2 or len(company_name) > 120 or len(username) < 3 or len(username) > 60 or len(password) < 12:
        return templates.TemplateResponse(request=request, name="register.html", context={"error": "Indique uma empresa válida e uma senha com pelo menos 12 caracteres.", "csrf_token": csrf_for(request)}, status_code=400)
    company_id, code = str(uuid4()), make_activation_code()
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    try:
        with connect() as db:
            company_cols = {r["name"] for r in db.execute("PRAGMA table_info(companies)").fetchall()}
            if "activation_code" in company_cols:
                # Legacy schema compatibility: store only an unusable tombstone in the old NOT NULL column.
                db.execute("INSERT INTO companies(id,name,username,password_hash,activation_code,activation_code_hash,activation_expires_at) VALUES(?,?,?,?,?,?,?)",
                           (company_id, company_name, username, hash_password(password), "INVALIDATED-"+company_id, hash_secret(code), expires))
            else:
                db.execute("INSERT INTO companies(id,name,username,password_hash,activation_code_hash,activation_expires_at) VALUES(?,?,?,?,?,?)",
                           (company_id, company_name, username, hash_password(password), hash_secret(code), expires))
            audit(db, company_id, username, "company.registered", company_id, "Empresa criada; código de ativação válido por 24 horas")
    except Exception:
        return templates.TemplateResponse(request=request, name="register.html", context={"error": "Não foi possível criar a conta. Verifique os dados e tente novamente.", "csrf_token": csrf_for(request)}, status_code=400)
    request.session.clear(); request.session["company_id"] = company_id; request.session["csrf_token"] = csrf_token()
    return templates.TemplateResponse(request=request, name="activation.html", context={"company_name": company_name, "activation_code": code, "csrf_token": csrf_for(request)})
 
@app.post("/login", response_class=HTMLResponse)
def login(request: Request, csrf_token_form: str = Form(..., alias="csrf_token"), username: str = Form(...), password: str = Form(...)):
    rate_limit(client_key(request, "login"), 8, 300)
    verify_csrf(request, csrf_token_form)
    with connect() as db: company = db.execute("SELECT * FROM companies WHERE username=?", (username.strip().lower(),)).fetchone()
    if not company or not verify_password(password, company["password_hash"]):
        # Generic response avoids revealing whether the username exists.
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Credenciais inválidas.", "csrf_token": csrf_for(request)}, status_code=401)
    request.session.clear(); request.session["company_id"] = company["id"]; request.session["csrf_token"] = csrf_token()
    return RedirectResponse("/dashboard", status_code=303)
 
@app.post("/logout")
def logout(request: Request, csrf_token_form: str = Form(..., alias="csrf_token")):
    verify_csrf(request, csrf_token_form); request.session.clear()
    return RedirectResponse("/", status_code=303)
 
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    company = require_company(request)
    with connect() as db:
        rows = db.execute("SELECT kind, COALESCE(SUM(amount_cents),0) AS total FROM transactions WHERE company_id=? GROUP BY kind", (company["id"],)).fetchall()
        recent = db.execute("SELECT * FROM transactions WHERE company_id=? ORDER BY occurred_at DESC LIMIT 8", (company["id"],)).fetchall()
        pending = db.execute("SELECT COUNT(*) FROM transactions WHERE company_id=? AND synced=0", (company["id"],)).fetchone()[0]
    totals = {r["kind"]: r["total"] for r in rows}; income, expense = totals.get("receita", 0), totals.get("despesa", 0)
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"company": company, "income": income, "expense": expense, "balance": income-expense, "recent": recent, "pending": pending, "csrf_token": csrf_for(request)})
 
@app.post("/financeiro/movimento")
def add_transaction(request: Request, csrf_token_form: str = Form(..., alias="csrf_token"), kind: str = Form(...), category: str = Form(...), description: str = Form(...), amount: str = Form(...)):
    company = require_company(request); verify_csrf(request, csrf_token_form)
    if kind not in {"receita", "despesa"}: raise HTTPException(400, "Tipo de movimento inválido")
    try: amount_cents = validate_amount(amount)
    except ValueError as exc: raise HTTPException(400, str(exc))
    category, description = category.strip(), description.strip()
    if not category or len(category) > 80 or not description or len(description) > 240: raise HTTPException(400, "Categoria ou descrição inválida")
    tx_id = str(uuid4()); occurred = utc_now_iso()
    with connect() as db:
        db.execute("INSERT INTO transactions(id,company_id,kind,category,description,amount_cents,source,synced,occurred_at,created_by) VALUES(?,?,?,?,?,?,?,1,?,?)",
                   (tx_id, company["id"], kind, category, description, amount_cents, "online", occurred, company["username"]))
        audit(db, company["id"], company["username"], "transaction.created", tx_id, json.dumps({"kind":kind,"amount_cents":amount_cents}))
    return RedirectResponse("/dashboard", status_code=303)
 
 
@app.post("/ativacao/novo", response_class=HTMLResponse)
def renew_activation(request: Request, csrf_token_form: str = Form(..., alias="csrf_token")):
    company = require_company(request); verify_csrf(request, csrf_token_form)
    code = make_activation_code(); expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    with connect() as db:
        db.execute("UPDATE companies SET activation_code_hash=?,activation_expires_at=?,activation_used_at=NULL WHERE id=?", (hash_secret(code), expires, company["id"]))
        audit(db, company["id"], company["username"], "activation.renewed", company["id"], "Novo código de uso único válido por 24 horas")
    return templates.TemplateResponse(request=request, name="activation.html", context={"company_name": company["name"], "activation_code": code, "csrf_token": csrf_for(request)})
 
@app.post("/api/device/activate")
def activate_device(payload: dict, request: Request):
    code = str(payload.get("activation_code", "")).strip().upper(); device_id = str(payload.get("device_id", ""))
    if len(device_id) < 8 or len(device_id) > 128: raise HTTPException(400, "device_id inválido")
    # Public activation endpoint only accepts TLS outside local development.
    if request.url.scheme != "https" and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}: raise HTTPException(400, "HTTPS obrigatório")
    now = utc_now_iso(); token = secrets.token_urlsafe(48)
    with connect() as db:
        company = db.execute("SELECT id,name,activation_expires_at,activation_used_at,activation_code_hash FROM companies WHERE activation_code_hash=?", (hash_secret(code),)).fetchone()
        if not company or company["activation_used_at"] or not company["activation_expires_at"] or company["activation_expires_at"] < now:
            raise HTTPException(403, "Código inválido, expirado ou já utilizado. Gere um novo código no portal autenticado.")
        existing = db.execute("SELECT company_id FROM activation_devices WHERE device_id=?", (device_id,)).fetchone()
        if existing: raise HTTPException(409, "Este dispositivo já está associado. Revogue-o antes de reativar.")
        db.execute("INSERT INTO activation_devices(device_id,company_id,token_hash) VALUES(?,?,?)", (device_id, company["id"], hash_secret(token)))
        db.execute("UPDATE companies SET activation_used_at=? WHERE id=?", (now, company["id"]))
        audit(db, company["id"], "device:"+device_id, "device.activated", device_id, "Ativação de uso único")
    return {"ok": True, "company_id": company["id"], "company_name": company["name"], "device_token": token}
 
@app.post("/api/sync/push")
def sync_push(payload: dict, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "): raise HTTPException(401, "Token do dispositivo obrigatório")
    token = authorization.split(" ",1)[1].strip(); device_id = str(payload.get("device_id", "")); events = payload.get("events", [])
    if not device_id or len(device_id) > 128 or not isinstance(events, list) or len(events) > 500: raise HTTPException(400, "Pedido de sincronização inválido (máximo 500 eventos)")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_000_000: raise HTTPException(413, "Lote demasiado grande")
    with connect() as db:
        device = db.execute("SELECT company_id,token_hash,revoked_at FROM activation_devices WHERE device_id=?", (device_id,)).fetchone()
        if not device or device["revoked_at"] or not hmac.compare_digest(device["token_hash"], hash_secret(token)):
            raise HTTPException(403, "Dispositivo não autenticado ou revogado")
        created, duplicate, conflicts, rejected = [], [], [], []
        for event in events:
            event_label = event.get("event_id", "desconhecido") if isinstance(event, dict) else "desconhecido"
            try:
                if not isinstance(event, dict): raise ValueError("evento deve ser objeto")
                event_id = str(UUID(str(event["event_id"])))
                kind = str(event["kind"])
                if kind not in {"receita", "despesa"}: raise ValueError("tipo inválido")
                raw_cents = event["amount_cents"]
                if isinstance(raw_cents, bool) or not isinstance(raw_cents, (int, float)) or not float(raw_cents).is_integer(): raise ValueError("amount_cents deve ser inteiro")
                cents = int(raw_cents)
                if cents <= 0 or cents > 10**12: raise ValueError("valor inválido")
                category = str(event.get("category", "Geral"))[:80].strip() or "Geral"
                description = str(event.get("description", ""))[:240].strip()
                if not description: raise ValueError("descrição obrigatória")
                occurred = normalize_timestamp(event.get("occurred_at"))
                existing = db.execute("SELECT kind,category,description,amount_cents,occurred_at FROM transactions WHERE company_id=? AND id=?", (device["company_id"], event_id)).fetchone()
                fingerprint = (kind, category, description, cents, occurred)
                if existing:
                    old = (existing["kind"], existing["category"], existing["description"], existing["amount_cents"], existing["occurred_at"])
                    if old == fingerprint: duplicate.append(event_id)
                    else: conflicts.append({"event_id":event_id,"reason":"ID já existe com conteúdo diferente"})
                    continue
                db.execute("INSERT INTO transactions(id,company_id,kind,category,description,amount_cents,source,synced,occurred_at,device_id) VALUES(?,?,?,?,?,?,?,1,?,?)",
                           (event_id, device["company_id"], kind, category, description, cents, "offline-sync", occurred, device_id))
                audit(db, device["company_id"], "device:"+device_id, "transaction.synced", event_id, json.dumps({"kind":kind,"amount_cents":cents}))
                created.append(event_id)
            except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation) as exc:
                rejected.append({"event_id": str(event_label)[:80], "reason": str(exc)[:160]})
        return {"ok": True, "created": created, "duplicate": duplicate, "conflicts": conflicts, "rejected": rejected}
 
@app.get("/health")
def health():
    try:
        with connect() as db: db.execute("SELECT 1").fetchone()
        return {"status":"ok", "database":"ok", "service":"AlphaTech-JLC Business Hybrid", "version":app.version}
    except Exception:
        raise HTTPException(503, "Serviço indisponível")
 
# FILE: alphatech_business_hybrid/app/security.py
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
 
 
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + digest.hex()
 
 
def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = password_hash.split("$", 2)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False
 
 
def make_activation_code() -> str:
    return "ATJ-" + secrets.token_hex(4).upper() + "-" + secrets.token_hex(4).upper()
 
 
def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
 
 
def secret_key() -> str:
    key = os.getenv("APP_SECRET_KEY", "").strip()
    insecure = {"DEV-ONLY-change-this-secret-before-production-7c91f2", "replace-with-a-long-random-secret", ""}
    if key in insecure or len(key) < 32:
        raise RuntimeError("APP_SECRET_KEY é obrigatória: defina um segredo aleatório com pelo menos 32 caracteres no ambiente/.env")
    return key
 
 
def csrf_token() -> str:
    return secrets.token_urlsafe(32)
 
 
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
 
 
def normalize_timestamp(value: str | None) -> str:
    if not value:
        return utc_now_iso()
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("data inválida")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("data inválida; use ISO-8601 com fuso horário") from exc
    if parsed.tzinfo is None:
        raise ValueError("a data deve incluir fuso horário")
    now = datetime.now(timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now + timedelta(minutes=10):
        raise ValueError("data futura não permitida")
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
 
/* FILE: alphatech_business_hybrid/app/static/style.css */
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@400;500;600;700;800&display=swap');
:root{--green:#20d69a;--green-dark:#087f5b;--navy:#102a2b;--ink:#172e2d;--muted:#748582;--line:#e4eeea;--bg:#f4f8f6;--white:#fff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:'DM Sans',sans-serif;min-height:100vh}.topbar{height:76px;padding:0 6vw;display:flex;align-items:center;justify-content:space-between;background:#fff;border-bottom:1px solid #e7efeb}.brand{display:flex;align-items:center;gap:10px;color:var(--navy);text-decoration:none;font-family:Manrope,sans-serif;font-weight:800;font-size:19px;letter-spacing:-.6px}.brand-mark{display:grid;place-items:center;width:36px;height:36px;border-radius:12px;background:#d8fff0;color:#07865f;font-size:28px}.brand-light{color:#0c9b70}.top-caption,.eyebrow{font-size:10px;letter-spacing:1.8px;font-weight:700;color:#77918a}.auth-layout{max-width:1160px;margin:56px auto;padding:0 24px;display:grid;grid-template-columns:1.05fr .85fr;gap:90px;align-items:center;min-height:570px}.welcome{padding:25px 0}.welcome h1{font:800 clamp(42px,5vw,64px)/1.08 Manrope,sans-serif;letter-spacing:-2.5px;margin:20px 0;color:#123b35}.welcome h1 span{color:#10a879}.welcome p{font-size:16px;line-height:1.8;color:#71827e;max-width:510px}.feature-list{display:grid;gap:15px;margin-top:35px;color:#34544d;font-size:14px}.feature-list div: first-letter{color:#0caa78}.auth-card{background:#fff;border:1px solid #e5eeea;border-radius:22px;padding:38px;box-shadow:0 20px 70px #173e2a0a}.card-icon,.success-icon{width:48px;height:48px;display:grid;place-items:center;border-radius:15px;background:#ddfff1;color:#07875f;font-size:26px}.auth-card h2{font:800 26px Manrope,sans-serif;margin:22px 0 8px;letter-spacing:-.8px}.muted{color:var(--muted);font-size:13px;line-height:1.65}.auth-card form{display:flex;flex-direction:column;margin-top:26px}.auth-card label,.transaction-form label{font-size:12px;font-weight:700;color:#38534c;margin:14px 0 8px}.auth-card input,.transaction-form input,.transaction-form select{width:100%;padding:14px 13px;border:1px solid #dfe9e5;border-radius:10px;background:#fbfdfc;color:#173c33;font:14px 'DM Sans',sans-serif;outline:none}.auth-card input:focus,.transaction-form input:focus,.transaction-form select:focus{border-color:#20c991;box-shadow:0 0 0 3px #20d69a1a}.primary{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#087f5b;color:#fff;border:0;border-radius:10px;padding:15px 17px;margin-top:23px;font-weight:700;font-size:13px;text-decoration:none;cursor:pointer;transition:.15s}.primary:hover{background:#066b4d}.auth-bottom{text-align:center;margin-top:23px;color:#71827e;font-size:12px}.auth-bottom a,.back{color:#07875f;font-weight:700;text-decoration:none}.secure-note{font-size:10px;color:#93a39e;text-align:center;margin-top:25px}.single-layout{max-width:540px;margin:55px auto;padding:0 20px}.wide{padding:40px}.back{font-size:12px}.wide .eyebrow{margin-top:28px}.tiny{font-size:11px;line-height:1.7;color:#879691;margin-top:20px}.activation-code{padding:20px 10px;margin-top:24px;border:1px dashed #19b983;background:#effff8;color:#087f5b;border-radius:12px;text-align:center;font:800 25px Manrope,sans-serif;letter-spacing:2px}.button-link{display:flex}.error{background:#fff0ef;color:#a43e38;padding:12px;border-radius:8px;font-size:12px;margin-top:18px}.dashboard{max-width:1320px;margin:38px auto;padding:0 28px 50px}.dash-heading{display:flex;justify-content:space-between;align-items:center;gap:20px}.dash-heading h1{font:800 30px Manrope,sans-serif;letter-spacing:-1px;margin:10px 0 5px}.secondary{border:1px solid #dce8e3;background:white;padding:11px 15px;border-radius:9px;color:#46645b;font-weight:700;cursor:pointer}.status-strip{display:flex;gap:24px;flex-wrap:wrap;background:#e9f8f1;border:1px solid #d9f0e5;border-radius:12px;padding:14px 18px;margin:25px 0;color:#456b5d;font-size:11px}.dot{display:inline-block;width:7px;height:7px;background:#12bd80;border-radius:50%;margin-right:7px}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.metric{background:white;border:1px solid #e5eeea;border-radius:15px;padding:23px;min-width:0}.metric>span{font-size:12px;color:#71827e}.metric strong{display:block;font:800 clamp(22px,2.2vw,30px) Manrope,sans-serif;margin:17px 0 9px;letter-spacing:-.8px;overflow-wrap:anywhere}.metric strong small{font:600 11px 'DM Sans',sans-serif;color:#82918c;letter-spacing:0}.metric em{font-style:normal;color:#8a9994;font-size:10px}.metric.highlight{background:#0b7456;border-color:#0b7456;color:white}.metric.highlight>span,.metric.highlight em,.metric.highlight strong small{color:#c6f5e3}.content-grid{display:grid;grid-template-columns:.8fr 1.2fr;gap:18px;margin-top:20px}.panel{background:white;border:1px solid #e5eeea;border-radius:15px;padding:24px;min-width:0}.panel-heading{display:flex;justify-content:space-between;align-items:center}.panel h2{font:800 17px Manrope,sans-serif;margin:0}.panel-heading p{margin:7px 0 0}.panel-symbol{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:#e7fbf2;color:#07875f;font-size:21px}.transaction-form{display:flex;flex-direction:column}.transaction-form label{margin-top:15px}.transaction-form .primary{width:100%;margin-top:20px}.table-wrap{overflow-x:auto;margin-top:18px}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;color:#8b9a95;font-size:10px;font-weight:600;padding:12px 8px;border-bottom:1px solid #edf2ef}td{padding:14px 8px;border-bottom:1px solid #f0f4f2}td small{display:block;color:#93a09b;font-size:10px;margin-top:5px}.tag{font-size:10px;border-radius:6px;padding:5px 7px}.tag.receita{background:#e4fbf0;color:#07875f}.tag.despesa{background:#fff0e9;color:#b76535}.amount{text-align:right;font-weight:700;white-space:nowrap}.empty{text-align:center;padding:40px 10px;color:#81918b}.empty div{font-size:28px;color:#18b984}.empty b{display:block;margin:10px}.empty p{font-size:12px}.module-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px}.module{background:#fff;border:1px solid #e5eeea;border-radius:13px;padding:20px;display:grid;gap:9px}.module>span{font-size:21px;color:#0c9b70}.module b{font-size:12px}.module small{font-size:10px;color:#93a09b}footer{text-align:center;color:#9aa8a2;font-size:10px;padding:24px}@media(max-width:900px){.auth-layout{gap:30px}.metric-grid{grid-template-columns:repeat(2,1fr)}.content-grid{grid-template-columns:1fr}.module-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:620px){.topbar{height:65px;padding:0 20px}.top-caption{display:none}.auth-layout{grid-template-columns:1fr;margin:22px auto;gap:20px}.welcome{padding:10px 0}.welcome h1{font-size:42px}.welcome p{font-size:14px}.auth-card{padding:25px}.single-layout{margin:25px auto}.wide{padding:27px}.dashboard{margin:24px auto;padding:0 15px}.dash-heading h1{font-size:23px}.metric-grid{gap:10px}.metric{padding:16px}.metric strong{font-size:22px}.status-strip{gap:12px}.panel{padding:18px}.module-grid{gap:10px}.module{padding:14px}}
 
<!-- FILE: alphatech_business_hybrid/app/templates/activation.html -->
{% extends "base.html" %}{% block title %}Código de ativação • AlphaTech-JLC{% endblock %}
{% block content %}<section class="single-layout"><div class="auth-card wide"><div class="success-icon">✓</div><div class="eyebrow">CONTA CRIADA</div><h2>{{ company_name }}</h2><p class="muted">Use este código para associar a aplicação local à conta online.</p><div class="activation-code">{{ activation_code }}</div><p class="tiny">O código expira em 24 horas e só pode ser usado uma vez. Guarde-o num local seguro; o sistema guarda apenas o seu hash.</p><a class="primary button-link" href="/dashboard">Abrir dashboard →</a></div></section>{% endblock %}
 
<!-- FILE: alphatech_business_hybrid/app/templates/base.html -->
<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}AlphaTech-JLC | Gestão Empresarial{% endblock %}</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header class="topbar"><a class="brand" href="/"><span class="brand-mark">α</span><span>AlphaTech<span class="brand-light">-JLC</span></span></a><span class="top-caption">GESTÃO EMPRESARIAL INTELIGENTE</span></header>
<main>{% block content %}{% endblock %}</main>
<footer>AlphaTech-JLC • Gestão financeira e operacional • Plataforma híbrida</footer>
</body></html>
 
<!-- FILE: alphatech_business_hybrid/app/templates/dashboard.html -->
{% extends "base.html" %}{% block title %}Dashboard • {{ company.name }}{% endblock %}
{% block content %}<div class="dashboard"><div class="dash-heading"><div><div class="eyebrow">PAINEL DE CONTROLO</div><h1>Olá, {{ company.name }}</h1><p class="muted">Visão geral da atividade e saúde financeira.</p></div><form method="post" action="/logout"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="secondary" type="submit">Terminar sessão</button></form></div><div class="status-strip"><span><i class="dot"></i> Serviço online</span><span>Estado da fila offline: consulte o cliente desktop</span><span>Sincronização disponível quando ligado</span><form method="post" action="/ativacao/novo"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><button class="secondary" type="submit">Gerar novo código de ativação</button></form></div><div class="metric-grid"><div class="metric"><span>Receitas registadas</span><strong>{{ '%.2f'|format(income/100) }} <small>MZN</small></strong><em>Entradas financeiras</em></div><div class="metric"><span>Despesas registadas</span><strong>{{ '%.2f'|format(expense/100) }} <small>MZN</small></strong><em>Saídas financeiras</em></div><div class="metric highlight"><span>Saldo operacional</span><strong>{{ '%.2f'|format(balance/100) }} <small>MZN</small></strong><em>Receitas menos despesas</em></div><div class="metric"><span>Fila offline local</span><strong>—</strong><em>Estado consultado na aplicação do PC</em></div></div><div class="content-grid"><section class="panel"><div class="panel-heading"><div><h2>Registar movimento</h2><p class="muted">Adicione uma entrada ou saída financeira.</p></div><span class="panel-symbol">＋</span></div><form class="transaction-form" method="post" action="/financeiro/movimento"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Tipo de movimento</label><select name="kind"><option value="receita">Receita / Entrada</option><option value="despesa">Despesa / Saída</option></select><label>Categoria</label><select name="category"><option>Vendas</option><option>Serviços</option><option>Salários</option><option>Renda</option><option>Transporte</option><option>Compras/Stock</option><option>Impostos</option><option>Outros</option></select><label>Descrição</label><input name="description" required maxlength="240" placeholder="Ex.: Venda de mercadoria"><label>Valor (MZN)</label><input name="amount" inputmode="decimal" required placeholder="0,00"><button class="primary" type="submit">Guardar movimento →</button></form></section><section class="panel"><div class="panel-heading"><div><h2>Movimentos recentes</h2><p class="muted">Últimas operações registadas.</p></div><span class="panel-symbol">↗</span></div>{% if recent %}<div class="table-wrap"><table><thead><tr><th>Movimento</th><th>Tipo</th><th>Valor</th></tr></thead><tbody>{% for item in recent %}<tr><td><b>{{ item['description'] }}</b><small>{{ item['category'] }} • {{ item['occurred_at'][:16] }}</small></td><td><span class="tag {{ item['kind'] }}">{{ item['kind'] }}</span></td><td class="amount">{{ '%.2f'|format(item['amount_cents']/100) }} MZN</td></tr>{% endfor %}</tbody></table></div>{% else %}<div class="empty"><div>◎</div><b>Ainda não há movimentos</b><p>Os registos financeiros aparecerão aqui.</p></div>{% endif %}</section></div><div class="module-grid"><div class="module"><span>▦</span><b>Vendas e clientes</b><small>Próximo módulo</small></div><div class="module"><span>◫</span><b>Stock e compras</b><small>Próximo módulo</small></div><div class="module"><span>♙</span><b>Recursos humanos</b><small>Próximo módulo</small></div><div class="module"><span>▤</span><b>Relatórios e auditoria</b><small>Próximo módulo</small></div></div></div>{% endblock %}
 
<!-- FILE: alphatech_business_hybrid/app/templates/login.html -->
{% extends "base.html" %}{% block title %}Entrar • AlphaTech-JLC{% endblock %}
{% block content %}<section class="auth-layout"><div class="welcome"><div class="eyebrow">CONTROLO. CLAREZA. CRESCIMENTO.</div><h1>A sua empresa.<br><span>Num só lugar.</span></h1><p>Gestão financeira e operacional com acompanhamento online e trabalho offline, pronto para sincronizar quando a ligação regressar.</p><div class="feature-list"><div>✓ Registo básico de receitas e despesas</div><div>✓ Resumo financeiro por empresa</div><div>✓ Acesso online do patronato</div><div>✓ Cliente offline com envio de movimentos</div></div></div><div class="auth-card"><div class="card-icon">↗</div><h2>Bem-vindo de volta</h2><p class="muted">Entre no painel da sua empresa.</p>{% if error %}<div class="error">{{ error }}</div>{% endif %}<form method="post" action="/login"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Username da empresa</label><input name="username" autocomplete="username" required placeholder="ex.: minhaempresa"><label>Senha</label><input name="password" type="password" autocomplete="current-password" required placeholder="A sua senha"><button class="primary" type="submit">Entrar no sistema <span>→</span></button></form><div class="auth-bottom">Ainda não tem conta? <a href="/registo">Registar empresa</a></div><div class="secure-note">● Sessão protegida • Acesso multiempresa</div></div></section>{% endblock %}
 
<!-- FILE: alphatech_business_hybrid/app/templates/register.html -->
{% extends "base.html" %}{% block title %}Registar empresa • AlphaTech-JLC{% endblock %}
{% block content %}<section class="single-layout"><div class="auth-card wide"><a class="back" href="/">← Voltar ao login</a><div class="eyebrow">COMECE AQUI</div><h2>Registar a sua empresa</h2><p class="muted">Crie a conta principal. Depois, use o código gerado para associar a instalação do computador.</p>{% if error %}<div class="error">{{ error }}</div>{% endif %}<form method="post" action="/registo"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><label>Nome da empresa ou instituição</label><input name="company_name" required maxlength="120" placeholder="Nome legal ou comercial"><label>Username da empresa</label><input name="username" required minlength="3" maxlength="60" placeholder="nome_da_empresa"><label>Senha principal (mínimo 12 caracteres)</label><input name="password" type="password" required minlength="12" autocomplete="new-password" placeholder="Crie uma senha forte"><button class="primary" type="submit">Criar conta e gerar código →</button></form><p class="tiny">Guarde o código de ativação. Não o partilhe publicamente.</p></div></section>{% endblock %}
 
# FILE: alphatech_business_hybrid/desktop_client.py
"""Cliente offline demonstrativo para Windows/Linux/macOS (Python + Tkinter + SQLite)."""
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse
import sqlite3
import urllib.request
import urllib.error
import uuid
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from datetime import datetime, timezone
 
DB = Path.home() / ".alphatech_jlc_business.db"
 
def db_connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS outbox (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL, amount_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    return conn
 
def setting(key, value=None):
    with db_connect() as db:
        if value is not None:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
 
def post_json(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token: headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=json.dumps(payload, allow_nan=False).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode())
 
class OfflineApp:
    def __init__(self, root):
        self.root = root
        root.title("AlphaTech-JLC | Cliente Offline")
        root.geometry("760x600")
        root.configure(bg="#f3f8f5")
        style = ttk.Style()
        style.configure("TButton", padding=8)
        tk.Label(root, text="α AlphaTech-JLC", bg="#f3f8f5", fg="#087f5b", font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=22, pady=(18, 0))
        tk.Label(root, text="GESTÃO EMPRESARIAL • MODO OFFLINE", bg="#f3f8f5", fg="#70857d", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=24, pady=(0, 14))
        config = ttk.LabelFrame(root, text="Ligação e ativação", padding=10); config.pack(fill="x", padx=20)
        self.url = tk.StringVar(value=setting("server_url") or "http://127.0.0.1:8000")
        self.code = tk.StringVar(value="")
        ttk.Label(config, text="URL do servidor").grid(row=0,column=0,sticky="w")
        ttk.Entry(config,textvariable=self.url,width=38).grid(row=0,column=1,padx=8,sticky="ew")
        ttk.Label(config, text="Código de ativação").grid(row=1,column=0,sticky="w",pady=6)
        ttk.Entry(config,textvariable=self.code,width=38).grid(row=1,column=1,padx=8,sticky="ew")
        ttk.Button(config,text="Ativar / testar ligação",command=self.activate).grid(row=0,column=2,rowspan=2,padx=8)
        config.columnconfigure(1,weight=1)
        form = ttk.LabelFrame(root,text="Novo movimento (funciona offline)",padding=10); form.pack(fill="x",padx=20,pady=14)
        self.kind=tk.StringVar(value="receita"); self.category=tk.StringVar(value="Vendas"); self.description=tk.StringVar(); self.amount=tk.StringVar()
        ttk.Label(form,text="Tipo").grid(row=0,column=0,sticky="w"); ttk.Combobox(form,textvariable=self.kind,values=("receita","despesa"),state="readonly",width=12).grid(row=1,column=0,padx=(0,8),sticky="ew")
        ttk.Label(form,text="Categoria").grid(row=0,column=1,sticky="w"); ttk.Combobox(form,textvariable=self.category,values=("Vendas","Serviços","Salários","Renda","Transporte","Stock","Impostos","Outros"),width=16).grid(row=1,column=1,padx=8,sticky="ew")
        ttk.Label(form,text="Descrição").grid(row=0,column=2,sticky="w"); ttk.Entry(form,textvariable=self.description,width=25).grid(row=1,column=2,padx=8,sticky="ew")
        ttk.Label(form,text="Valor MZN").grid(row=0,column=3,sticky="w"); ttk.Entry(form,textvariable=self.amount,width=12).grid(row=1,column=3,padx=8,sticky="ew")
        ttk.Button(form,text="Guardar",command=self.save).grid(row=1,column=4,padx=8)
        listing=ttk.LabelFrame(root,text="Movimentos locais",padding=10); listing.pack(fill="both",expand=True,padx=20,pady=(0,10))
        self.tree=ttk.Treeview(listing,columns=("type","desc","value","status"),show="headings",height=8)
        for col,title,width in (("type","Tipo",85),("desc","Descrição",330),("value","Valor (MZN)",120),("status","Estado",100)):
            self.tree.heading(col,text=title); self.tree.column(col,width=width)
        self.tree.pack(fill="both",expand=True)
        bottom=ttk.Frame(root); bottom.pack(fill="x",padx=20,pady=(0,16))
        self.status=tk.StringVar(value="Os movimentos ficam guardados neste PC mesmo sem internet.")
        ttk.Label(bottom,textvariable=self.status).pack(side="left",fill="x",expand=True)
        ttk.Button(bottom,text="Sincronizar agora",command=self.sync).pack(side="right")
        self.refresh()
 
    def activate(self):
        base=self.url.get().strip().rstrip("/"); code=self.code.get().strip()
        if not base or not code: messagebox.showwarning("Dados em falta","Indique URL e código de ativação."); return
        parsed=urlparse(base)
        if not (parsed.scheme == "https" and parsed.hostname or parsed.scheme == "http" and parsed.hostname in {"localhost","127.0.0.1","::1"}):
            messagebox.showerror("URL insegura","Use HTTPS. HTTP só é permitido para localhost em testes."); return
        device=setting("device_id") or str(uuid.uuid4())
        try:
            result=post_json(base.rstrip("/")+"/api/device/activate",{"activation_code":code,"device_id":device})
            # Save settings only after successful validation/activation; never persist the one-time code.
            setting("server_url",base.rstrip("/")); setting("device_id",device)
            setting("device_token",result["device_token"]); setting("company_id",result["company_id"]); setting("company_name",result["company_name"])
            self.code.set("")
            self.status.set("Ativado: "+result["company_name"]); messagebox.showinfo("Ativação concluída","Este dispositivo foi associado à empresa "+result["company_name"])
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode("utf-8",errors="replace")[:500]
            messagebox.showerror(f"Erro HTTP {exc.code}", detail or str(exc))
        except Exception as exc: messagebox.showerror("Não foi possível ativar",f"Confirme a URL e a ligação.\n\n{exc}")
 
    def save(self):
        try:
            raw=self.amount.get().strip().replace(" ","")
            if "," in raw and "." in raw: raise ValueError("Use apenas um separador decimal.")
            value=Decimal(raw.replace(",","."))
            if not value.is_finite(): raise ValueError("O valor deve ser finito.")
            cents=int((value*100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        except (ValueError, InvalidOperation): messagebox.showerror("Valor inválido","Introduza um valor decimal válido, por exemplo 1250,50."); return
        if cents<=0 or not self.description.get().strip(): messagebox.showerror("Dados inválidos","Descrição e valor positivo são obrigatórios."); return
        with db_connect() as db:
            db.execute("INSERT INTO outbox(event_id,kind,category,description,amount_cents,occurred_at,synced) VALUES(?,?,?,?,?,?,0)",(str(uuid.uuid4()),self.kind.get(),self.category.get(),self.description.get().strip()[:240],cents,datetime.now(timezone.utc).isoformat()))
        self.description.set(""); self.amount.set(""); self.refresh(); self.status.set("Movimento guardado localmente. Pode sincronizar quando houver internet.")
 
    def refresh(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        with db_connect() as db:
            rows=db.execute("SELECT * FROM outbox ORDER BY occurred_at DESC LIMIT 100").fetchall()
            pending=db.execute("SELECT COUNT(*) FROM outbox WHERE synced=0").fetchone()[0]
        for row in rows: self.tree.insert("","end",values=(row["kind"],row["description"],f'{row["amount_cents"]/100:.2f}',"Sincronizado" if row["synced"] else "Pendente"))
        self.status.set(f"{pending} movimento(s) pendente(s) • Dados guardados neste PC")
 
    def sync(self):
        base=setting("server_url"); device=setting("device_id"); token=setting("device_token")
        if not base or not device or not token: messagebox.showwarning("Ativação necessária","Ative primeiro este dispositivo com o código da empresa."); return
        with db_connect() as db: rows=db.execute("SELECT * FROM outbox WHERE synced=0 ORDER BY occurred_at LIMIT 500").fetchall()
        if not rows: self.status.set("Tudo sincronizado."); return
        payload={"device_id":device,"events":[{"event_id":r["event_id"],"kind":r["kind"],"category":r["category"],"description":r["description"],"amount_cents":r["amount_cents"],"occurred_at":r["occurred_at"]} for r in rows]}
        try:
            result=post_json(base.rstrip("/")+"/api/sync/push",payload,token=token)
            accepted=set(result.get("created",[])) | set(result.get("duplicate",[]))
            with db_connect() as db:
                for event_id in accepted: db.execute("UPDATE outbox SET synced=1 WHERE event_id=?",(event_id,))
            self.refresh(); self.status.set(f"Sincronização concluída: {len(accepted)} evento(s) aceites; {len(result.get('rejected',[]))} rejeitado(s).")
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode("utf-8",errors="replace")[:500]
            messagebox.showerror(f"Sincronização rejeitada (HTTP {exc.code})",detail or str(exc))
            self.status.set("Há eventos pendentes. Consulte o erro e corrija antes de repetir.")
        except urllib.error.URLError:
            messagebox.showwarning("Sem ligação","Os dados continuam guardados localmente. Tente novamente quando houver internet."); self.status.set("Offline: operações preservadas no PC.")
        except Exception as exc: messagebox.showerror("Erro de sincronização",str(exc))
 
if __name__ == "__main__":
    root=tk.Tk(); OfflineApp(root); root.mainloop()
 
# FILE: alphatech_business_hybrid/requirements.txt
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
jinja2>=3.1,<4.0
python-multipart>=0.0.9
itsdangerous>=2.2
pytest>=8.0
httpx>=0.27
 
# FILE: alphatech_business_hybrid/scripts/backup_sqlite.py
"""Create a consistent SQLite backup. Not encrypted; protect destination storage."""
import argparse
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
 
 
def main():
    parser = argparse.ArgumentParser(description="Backup consistente da base SQLite AlphaTech-JLC")
    parser.add_argument("--source", default=os.getenv("DATABASE_PATH", "./business_hybrid.db"))
    parser.add_argument("--dest", default="./backups")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"Base de dados não encontrada: {source}")
    dest_dir = Path(args.dest).resolve(); dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"business_hybrid_{stamp}.sqlite3"
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            target.unlink(missing_ok=True)
            raise SystemExit(f"Backup inválido: {check}")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (target.with_suffix(target.suffix + ".sha256")).write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    print(f"Backup criado: {target}")
    print(f"SHA-256: {digest}")
    print("AVISO: este script não cifra o backup; guarde-o num destino protegido e externo ao servidor.")
 
if __name__ == "__main__": main()
 
# FILE: alphatech_business_hybrid/scripts/load_test.py
"""Basic concurrent health-endpoint smoke test; not a full business/load certification."""
import argparse
import concurrent.futures
import time
import urllib.error
import urllib.request
 
 
def check(url, timeout):
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
            return response.status, time.perf_counter() - start, len(body)
    except Exception:
        return 0, time.perf_counter() - start, 0
 
 
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1: raise SystemExit("requests e concurrency devem ser positivos")
    start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda _: check(args.url, args.timeout), range(args.requests)))
    elapsed = time.perf_counter() - start
    ok = [r for r in results if r[0] == 200]
    latencies = sorted(r[1] for r in results)
    p95 = latencies[min(len(latencies)-1, int(len(latencies)*0.95))]
    print(f"Pedidos: {args.requests}; concorrência: {args.concurrency}; HTTP 200: {len(ok)}; erros: {len(results)-len(ok)}")
    print(f"Duração: {elapsed:.2f}s; throughput: {len(results)/elapsed:.2f} req/s; latência p95: {p95*1000:.1f} ms")
    if len(ok) != len(results): raise SystemExit(1)
    print("NOTA: teste limitado ao endpoint /health; não substitui testes de carga de login, sincronização, BD e fluxos financeiros.")
 
if __name__ == "__main__": main()
 
# FILE: alphatech_business_hybrid/tests/test_smoke.py
import os
import tempfile
import re
import json
import uuid
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timezone
from fastapi.testclient import TestClient
 
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_PATH"] = _db_path
os.environ["APP_SECRET_KEY"] = "test-secret-key-for-tests-only-123456789-abcdef"
os.environ["APP_MODE"] = "development"
from app.main import app, _RATE
 
 
def csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not present in form"
    return match.group(1)
 
 
def register_company(client, name="Empresa Teste", username=None):
    _RATE.clear()  # isolate test cases; rate-limit behavior is not under test here
    username = username or ("empresa_" + uuid.uuid4().hex[:10])
    page = client.get("/registo")
    response = client.post("/registo", data={
        "csrf_token": csrf(page.text), "company_name": name, "username": username,
        "password": "SenhaMuitoForte123!"
    })
    assert response.status_code == 200, response.text
    code_match = re.search(r"ATJ-[A-F0-9]+-[A-F0-9]+", response.text)
    assert code_match, response.text
    return code_match.group(0)
 
 
def test_home_and_health():
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/").headers["x-frame-options"] == "DENY"
 
 
def test_company_registration_and_dashboard_and_csrf():
    with TestClient(app, base_url="http://localhost") as client:
        register_company(client)
        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "Empresa Teste" in dashboard.text
        # Missing CSRF token must be rejected.
        bad = client.post("/financeiro/movimento", data={"kind":"receita","category":"Vendas","description":"Venda","amount":"10,00"})
        assert bad.status_code == 422 or bad.status_code == 403
 
 
def test_activation_is_one_time_and_sync_requires_bearer_token():
    with TestClient(app, base_url="http://localhost") as client:
        code = register_company(client)
        device_id = str(uuid.uuid4())
        activated = client.post("/api/device/activate", json={"activation_code": code, "device_id": device_id})
        assert activated.status_code == 200, activated.text
        data = activated.json()
        assert data["device_token"]
        second_device = client.post("/api/device/activate", json={"activation_code": code, "device_id": str(uuid.uuid4())})
        assert second_device.status_code == 403
        event = {"event_id": str(uuid.uuid4()), "kind":"receita", "category":"Vendas", "description":"Venda offline", "amount_cents": 12550, "occurred_at": datetime.now(timezone.utc).isoformat()}
        denied = client.post("/api/sync/push", json={"device_id":device_id,"events":[event]})
        assert denied.status_code == 401
        result = client.post("/api/sync/push", headers={"Authorization":"Bearer "+data["device_token"]}, json={"device_id":device_id,"events":[event]})
        assert result.status_code == 200, result.text
        assert result.json()["created"] == [event["event_id"]]
        again = client.post("/api/sync/push", headers={"Authorization":"Bearer "+data["device_token"]}, json={"device_id":device_id,"events":[event]})
        assert again.json()["duplicate"] == [event["event_id"]]
        conflict_event = dict(event, amount_cents=99999)
        conflict = client.post("/api/sync/push", headers={"Authorization":"Bearer "+data["device_token"]}, json={"device_id":device_id,"events":[conflict_event]})
        assert conflict.json()["conflicts"][0]["event_id"] == event["event_id"]
 
 
def test_sync_rejects_non_finite_amount_without_rolling_back_other_events():
    with TestClient(app, base_url="http://localhost") as client:
        code = register_company(client, "Empresa Finita", "empresa_finita")
        device_id = str(uuid.uuid4())
        activated = client.post("/api/device/activate", json={"activation_code":code,"device_id":device_id}).json()
        good = {"event_id":str(uuid.uuid4()),"kind":"despesa","category":"Renda","description":"Aluguer","amount_cents":12000,"occurred_at":datetime.now(timezone.utc).isoformat()}
        bad = {"event_id":str(uuid.uuid4()),"kind":"receita","category":"Vendas","description":"Infinito","amount_cents":float("inf"),"occurred_at":datetime.now(timezone.utc).isoformat()}
        response = client.post("/api/sync/push", headers={"Authorization":"Bearer "+activated["device_token"], "Content-Type":"application/json"}, content=json.dumps({"device_id":device_id,"events":[bad,good]}, allow_nan=True))
        assert response.status_code == 200, response.text
        assert len(response.json()["created"]) == 1
        assert len(response.json()["rejected"]) == 1
 
 
def test_money_validation_and_tenant_isolation():
    from app.main import validate_amount
    assert validate_amount("1.005") == 101
    assert validate_amount("0,285") == 29
    for value in ("inf", "Infinity", "1e400", "1.234,56"):
        try:
            validate_amount(value)
            assert False, f"should reject {value}"
        except ValueError:
            pass
    with TestClient(app, base_url="http://localhost") as client_a, TestClient(app, base_url="http://localhost") as client_b:
        # Tenant isolation: company B cannot see company A's dashboard without its own session.
        register_company(client_a, "Empresa A", "empresa_a")
        assert client_b.get("/dashboard").status_code == 401
 
 
def test_device_cannot_be_reassigned_to_another_company():
    with TestClient(app, base_url="http://localhost") as client:
        code_a = register_company(client, "Empresa A", "empresa_a_reassign")
        device_id = str(uuid.uuid4())
        first = client.post("/api/device/activate", json={"activation_code":code_a,"device_id":device_id})
        assert first.status_code == 200
        code_b = register_company(client, "Empresa B", "empresa_b_reassign")
        attempt = client.post("/api/device/activate", json={"activation_code":code_b,"device_id":device_id})
        assert attempt.status_code == 409
 
 
def test_sync_idempotency_is_scoped_to_company():
    with TestClient(app, base_url="http://localhost") as client:
        code_a = register_company(client, "Tenant A", "tenant_a_scope")
        dev_a = str(uuid.uuid4())
        token_a = client.post("/api/device/activate", json={"activation_code":code_a,"device_id":dev_a}).json()["device_token"]
        code_b = register_company(client, "Tenant B", "tenant_b_scope")
        dev_b = str(uuid.uuid4())
        token_b = client.post("/api/device/activate", json={"activation_code":code_b,"device_id":dev_b}).json()["device_token"]
        shared_event_id = str(uuid.uuid4())
        event_a = {"event_id":shared_event_id,"kind":"receita","category":"Vendas","description":"A","amount_cents":100,"occurred_at":datetime.now(timezone.utc).isoformat()}
        event_b = dict(event_a, description="B", amount_cents=200)
        res_a = client.post("/api/sync/push", headers={"Authorization":"Bearer "+token_a}, json={"device_id":dev_a,"events":[event_a]})
        res_b = client.post("/api/sync/push", headers={"Authorization":"Bearer "+token_b}, json={"device_id":dev_b,"events":[event_b]})
        assert res_a.json()["created"] == [shared_event_id]
        assert res_b.json()["created"] == [shared_event_id]
