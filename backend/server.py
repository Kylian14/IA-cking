"""
Application FastAPI : élève (port 8000) - admin (port 8001)
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import os
import sys
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadData, BadSignature, URLSafeTimedSerializer
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

import db
import llm_client
from levels import (
    LEVELS,
    generate_secret,
    get_level,
    input_blocked,
    public_level_info,
    strip_secret_from_output,
)
from ws_manager import admin_ws, student_ws

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("workshop")
audit = logging.getLogger("workshop.audit")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
STUDENT_PORT = int(os.getenv("STUDENT_PORT", "8000"))
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8001"))
HOST = os.getenv("HOST", "127.0.0.1")
MESSAGE_MAX_LEN = 2000
HISTORY_MAX_PAIRS = max(0, int(os.getenv("HISTORY_MAX_PAIRS", "10")))

SECURE_COOKIES = os.getenv("SECURE_COOKIES", "0") == "1"
COOKIE_TTL_SEC = int(os.getenv("COOKIE_TTL_SEC", str(60 * 60 * 24)))
# Domaine du cookie. En prod multi-sous-domaines (atelier.example.com +
# admin.example.com), mettre `.example.com` pour partager le cookie
# admin entre les deux apps (sinon le mode démo accessible côté élève
# ne reçoit pas le cookie admin). Laisser vide pour host-only en dev.
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", "") or None

MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(64 * 1024)))

MAX_LLM_CALLS_PER_SESSION = int(os.getenv("MAX_LLM_CALLS_PER_SESSION", "300"))

LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "5/minute")
ADMIN_LOGIN_RATE_LIMIT = os.getenv("ADMIN_LOGIN_RATE_LIMIT", "10/minute")
CHAT_RATE_LIMIT = os.getenv("CHAT_RATE_LIMIT", "20/minute")
VERIFY_RATE_LIMIT = os.getenv("VERIFY_RATE_LIMIT", "5/minute")

WS_ALLOWED_ORIGINS = {
    o.strip() for o in os.getenv("WS_ALLOWED_ORIGINS", "").split(",") if o.strip()
}
WS_MAX_PER_TOKEN = int(os.getenv("WS_MAX_PER_TOKEN", "5"))
WS_IDLE_TIMEOUT_SEC = int(os.getenv("WS_IDLE_TIMEOUT_SEC", "120"))

FRONTEND_DIR = os.getenv("FRONTEND_DIR", os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "frontend", "public")
))
SERVE_STATIC = os.path.isdir(FRONTEND_DIR)


def fail_fast() -> None:
    if not ADMIN_PASSWORD or ADMIN_PASSWORD == "changeme":
        sys.stderr.write(
            "ERREUR : ADMIN_PASSWORD doit être renseigné dans .env "
            "(et différent de 'changeme').\n"
        )
        sys.exit(1)
    if not SESSION_SECRET or len(SESSION_SECRET) < 16:
        sys.stderr.write(
            "ERREUR : SESSION_SECRET doit être renseigné dans .env "
            "(au moins 16 caractères aléatoires).\n"
        )
        sys.exit(1)
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("sk-or-your"):
        sys.stderr.write(
            "ERREUR : OPENROUTER_API_KEY doit être renseigné dans .env "
            "(récupère ta clé sur https://openrouter.ai/keys).\n"
        )
        sys.exit(1)


serializer = URLSafeTimedSerializer(SESSION_SECRET or "dev-secret-only-for-init", salt="workshop")

STUDENT_COOKIE = "workshop_student"
ADMIN_COOKIE = "workshop_admin"

def _cookie_kw(samesite: str = "lax", path: str = "/") -> dict:
    kw = dict(
        httponly=True,
        samesite=samesite,
        secure=SECURE_COOKIES,
        max_age=COOKIE_TTL_SEC,
        path=path,
    )
    if COOKIE_DOMAIN:
        kw["domain"] = COOKIE_DOMAIN
    return kw

COOKIE_KW_STUDENT = _cookie_kw(samesite="lax")
COOKIE_KW_ADMIN = _cookie_kw(samesite="strict")


def client_ip(request: Request) -> str:
    """IP réelle du client. Derrière notre unique ingress nginx, on lit
    X-Real-IP (nginx l'écrase avec $remote_addr, donc non-spoofable) ; sinon
    (dev sans nginx) on retombe sur l'adresse de connexion directe.
    On évite X-Forwarded-For : nginx l'APPENDE, donc son préfixe est contrôlé
    par le client, l'utiliser comme clé de rate-limit serait contournable."""
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=client_ip, default_limits=[])


# ============================================================
#  Helpers d'authentification
# ============================================================

def _safe_str(value, max_len: int = 4096) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    return value[:max_len]


def make_student_cookie(token: str) -> str:
    return serializer.dumps({"token": token})


def read_student_cookie(value: str) -> Optional[str]:
    try:
        data = serializer.loads(value, max_age=COOKIE_TTL_SEC)
        return data.get("token") if isinstance(data, dict) else None
    except (BadSignature, BadData, Exception):
        return None


def get_student_token(request: Request) -> str:
    raw = request.cookies.get(STUDENT_COOKIE)
    token = read_student_cookie(raw) if raw else None
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non authentifié.")
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session inconnue.")
    if sess["revoked"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session révoquée.")
    return token


def get_demo_token(request: Request) -> str:
    """Mode démo : pas d'auth, on travaille toujours sur la session DEMO."""
    return db.DEMO_TOKEN


def make_admin_cookie() -> str:
    return serializer.dumps({"admin": True})


def read_admin_cookie(value: str) -> bool:
    try:
        data = serializer.loads(value, max_age=COOKIE_TTL_SEC)
        return bool(isinstance(data, dict) and data.get("admin"))
    except (BadSignature, BadData, Exception):
        return False


def require_admin(request: Request) -> None:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw or not read_admin_cookie(raw):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin requis.")


# ============================================================
#  Logique partagée
# ============================================================

def ensure_level_secret(token: str, level_id: int) -> dict:
    """Crée la progression du niveau pour ce token si nécessaire, renvoie l'enregistrement."""
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    progress = db.get_progress(token, level_id)
    if progress is None:
        secret = generate_secret()
        db.ensure_progress(token, level_id, secret)
        progress = db.get_progress(token, level_id)
    return dict(progress) 


def check_progression(token: str, level_id: int, allow_demo_skip: bool = True) -> None:
    """Vérifie que tous les niveaux précédents sont résolus.
    En mode démo on peut sauter"""
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(401, "Session inconnue.")
    if sess["is_demo"] and allow_demo_skip:
        return
    for n in range(1, level_id):
        if not db.is_level_solved(token, n):
            raise HTTPException(403, f"Niveau {n} pas encore résolu.")


async def call_model_for_level(level: dict, token: str, level_id: int, user_message: str) -> str:
    """Appel OpenRouter dans un thread pour ne pas bloquer la boucle async."""
    progress = db.get_progress(token, level_id)
    if not progress:
        raise HTTPException(500, "Progression introuvable.")
    secret = progress["secret"]
    system_prompt = level["system_prompt_template"].format(secret=secret)
    history = db.list_chat_history_for_groq(token, level_id, HISTORY_MAX_PAIRS)
    response = await asyncio.to_thread(
        llm_client.chat,
        level["model"],
        system_prompt,
        history,
        user_message,
    )
    if level["output_filter_secret"]:
        response = strip_secret_from_output(response, secret)
    return response


async def broadcast_admin(event: str, **payload) -> None:
    await admin_ws.broadcast({"event": event, **payload})


# ============================================================
#  Middlewares partagés
# ============================================================

class BodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload trop volumineux."}, status_code=413)
        return await call_next(request)


def _setup_app(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_middleware(BodySizeMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            {"detail": "Trop de requêtes, ralentis un peu."},
            status_code=429,
        )


# ============================================================
#  Application ÉLÈVE (port 8000)
# ============================================================

student_app = FastAPI(title="IA'cking : Élève")
_setup_app(student_app)


@student_app.get("/health")
async def student_health():
    return {"ok": True}


@student_app.get("/")
async def student_index():
    if not SERVE_STATIC:
        raise HTTPException(404, "Frontend non monté côté backend (servi par nginx).")
    return FileResponse(os.path.join(FRONTEND_DIR, "student.html"))


@student_app.get("/demo")
async def student_demo(request: Request):
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw or not read_admin_cookie(raw):
        return RedirectResponse(url="/", status_code=302)
    if not SERVE_STATIC:
        raise HTTPException(404, "Frontend non monté côté backend (servi par nginx).")
    return FileResponse(os.path.join(FRONTEND_DIR, "demo.html"))


if SERVE_STATIC:
    student_app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@student_app.post("/api/login")
@limiter.limit(LOGIN_RATE_LIMIT)  # C2 : anti-bruteforce
async def student_login(request: Request, payload: dict, response: Response):
    token = _safe_str(payload.get("token")).strip()
    name = _safe_str(payload.get("name")).strip()
    if not token.isdigit() or len(token) != 6:
        raise HTTPException(400, "Le token doit faire 6 chiffres.")
    if not name:
        raise HTTPException(400, "Le prénom est obligatoire.")
    sess = db.get_session(token)
    if not sess or sess["revoked"] or sess["is_demo"]:
        raise HTTPException(401, "Token inconnu ou indisponible.")
    db.set_session_name(token, name[:50])
    response.set_cookie(STUDENT_COOKIE, make_student_cookie(token), **COOKIE_KW_STUDENT)
    await broadcast_admin("session_login", token=token, name=name[:50])
    return {"ok": True, "name": name[:50]}


@student_app.post("/api/logout")
async def student_logout(response: Response):
    response.delete_cookie(STUDENT_COOKIE, path="/", samesite="lax", domain=COOKIE_DOMAIN)
    if COOKIE_DOMAIN:
        response.delete_cookie(STUDENT_COOKIE, path="/", samesite="lax")
    return {"ok": True}


@student_app.get("/api/can-demo")
async def can_demo(request: Request):
    """Indique si l'utilisateur courant a un cookie admin valide.
    Utilisé par la page login élève pour afficher un bouton « accéder à
    la démo » quand l'encadrant arrive sur le domaine élève."""
    raw = request.cookies.get(ADMIN_COOKIE)
    return {"admin": bool(raw and read_admin_cookie(raw))}


@student_app.get("/api/me")
async def student_me(request: Request):
    token = get_student_token(request)
    sess = db.get_session(token)
    return {"token": token, "name": sess["name"] if sess else None}


@student_app.get("/api/levels")
async def student_levels(request: Request):
    token = get_student_token(request)
    return _build_levels_payload(token)


def _build_levels_payload(token: str, demo: bool = False) -> dict:
    progress_rows = {p["level_id"]: p for p in db.list_progress(token)}
    levels_out = []
    unlocked = True
    for lvl in LEVELS:
        lid = lvl["id"]
        prog = progress_rows.get(lid)
        is_solved = bool(prog and prog["solved"])
        is_unlocked = unlocked or demo
        levels_out.append({
            **public_level_info(lvl),
            "unlocked": is_unlocked,
            "solved": is_solved,
            "attempts": prog["attempts"] if prog else 0,
            "hints_received": prog["hints_received"] if prog else 0,
        })
        if not is_solved:
            unlocked = False
    return {"levels": levels_out}


@student_app.get("/api/levels/{level_id}/messages")
async def student_messages(request: Request, level_id: int):
    token = get_student_token(request)
    check_progression(token, level_id, allow_demo_skip=False)
    ensure_level_secret(token, level_id)
    return {"messages": db.list_messages(token, level_id)}


@student_app.post("/api/levels/{level_id}/chat")
@limiter.limit(CHAT_RATE_LIMIT)
async def student_chat(request: Request, level_id: int, payload: dict):
    token = get_student_token(request)
    return await _handle_chat(token, level_id, payload, demo=False)


@student_app.post("/api/levels/{level_id}/verify")
@limiter.limit(VERIFY_RATE_LIMIT) 
async def student_verify(request: Request, level_id: int, payload: dict):
    token = get_student_token(request)
    return await _handle_verify(token, level_id, payload, demo=False)


@student_app.get("/api/levels/{level_id}/hint")
async def student_hint(request: Request, level_id: int):
    get_student_token(request)
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    return {"hint": lvl["hint"]}


@student_app.post("/api/levels/{level_id}/help")
@limiter.limit("3/minute")
async def student_help(request: Request, level_id: int):
    """L'élève signale qu'il est bloqué → notification au dashboard admin
    via WebSocket. Pas de persistance : transient, dégagé à la sélection
    par l'admin ou au reload du dashboard."""
    token = get_student_token(request)
    if not get_level(level_id):
        raise HTTPException(404, "Niveau inconnu.")
    sess = db.get_session(token)
    await broadcast_admin(
        "help_requested",
        token=token,
        level_id=level_id,
        name=(sess["name"] if sess else None),
    )
    return {"ok": True}


@student_app.get("/api/levels/{level_id}/model")
async def student_model(request: Request, level_id: int):
    get_student_token(request)
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    return {"model": lvl["model"]}


def _ws_origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin", "")
    if not origin:
        return True
    if WS_ALLOWED_ORIGINS:
        return origin in WS_ALLOWED_ORIGINS
    try:
        o_host = urlparse(origin).hostname
        req_host = (ws.headers.get("host") or "").split(":")[0]
        return bool(o_host) and o_host == req_host
    except Exception:
        return False


async def _ws_keepalive_loop(ws: WebSocket) -> None:
    while True:
        try:
            await asyncio.wait_for(ws.receive_text(), timeout=WS_IDLE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            try:
                await ws.send_text('{"event":"ping"}')
            except Exception:
                return


@student_app.websocket("/api/ws")
async def student_websocket(ws: WebSocket):
    if not _ws_origin_allowed(ws):
        await ws.close(code=1008)
        return
    raw = ws.cookies.get(STUDENT_COOKIE)
    token = read_student_cookie(raw) if raw else None
    if not token:
        await ws.close(code=1008)
        return
    sess = db.get_session(token)
    if not sess or sess["revoked"]:
        await ws.close(code=1008)
        return
    if student_ws.count(token) >= WS_MAX_PER_TOKEN:
        await ws.close(code=1013)
        return
    await student_ws.connect(token, ws)
    try:
        await _ws_keepalive_loop(ws)
    except WebSocketDisconnect:
        pass
    finally:
        await student_ws.disconnect(token, ws)


# --- Démo ---

@student_app.get("/api/demo/me")
async def demo_me(request: Request):
    require_admin(request)
    token = get_demo_token(request)
    return {"token": token, "name": "Démo"}


@student_app.get("/api/demo/levels")
async def demo_levels(request: Request):
    require_admin(request)
    token = get_demo_token(request)
    return _build_levels_payload(token, demo=True)


@student_app.get("/api/demo/levels/{level_id}/messages")
async def demo_messages(request: Request, level_id: int):
    require_admin(request)
    token = get_demo_token(request)
    ensure_level_secret(token, level_id)
    return {"messages": db.list_messages(token, level_id)}


@student_app.post("/api/demo/levels/{level_id}/chat")
@limiter.limit(CHAT_RATE_LIMIT)
async def demo_chat(request: Request, level_id: int, payload: dict):
    require_admin(request)
    token = get_demo_token(request)
    return await _handle_chat(token, level_id, payload, demo=True)


@student_app.post("/api/demo/levels/{level_id}/verify")
@limiter.limit(VERIFY_RATE_LIMIT)
async def demo_verify(request: Request, level_id: int, payload: dict):
    require_admin(request)
    token = get_demo_token(request)
    return await _handle_verify(token, level_id, payload, demo=True)


@student_app.get("/api/demo/levels/{level_id}/hint")
async def demo_hint(request: Request, level_id: int):
    require_admin(request)
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    return {"hint": lvl["hint"]}


@student_app.get("/api/demo/levels/{level_id}/model")
async def demo_model(request: Request, level_id: int):
    require_admin(request)
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    return {"model": lvl["model"]}


# --- Logique commune chat / verify ---

async def _handle_chat(token: str, level_id: int, payload: dict, demo: bool) -> dict:
    user_message = _safe_str(payload.get("message"), MESSAGE_MAX_LEN + 1).strip()
    if not user_message:
        raise HTTPException(400, "Message vide.")
    if len(user_message) > MESSAGE_MAX_LEN:
        raise HTTPException(400, f"Message trop long (max {MESSAGE_MAX_LEN}).")
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    check_progression(token, level_id, allow_demo_skip=demo)
    ensure_level_secret(token, level_id)
    db.touch_session(token)

    if not demo and db.count_llm_calls(token) >= MAX_LLM_CALLS_PER_SESSION:
        raise HTTPException(
            429,
            f"Budget de {MAX_LLM_CALLS_PER_SESSION} appels modèle atteint pour cette session.",
        )

    # Filtre d'entrée
    blocked = input_blocked(user_message, lvl["input_banned"])
    user_msg_record = db.add_message(token, level_id, "user", user_message)
    await broadcast_admin(
        "message",
        token=token,
        level_id=level_id,
        message=user_msg_record,
    )
    if blocked is not None:
        refusal = (
            "[FILTRE D'ENTREE] Ton message contient un mot interdit. "
            "Reformule en évitant les termes liés au secret."
        )
        assistant_msg = db.add_message(token, level_id, "assistant", refusal)
        await broadcast_admin(
            "message",
            token=token,
            level_id=level_id,
            message=assistant_msg,
        )
        return {"reply": refusal, "blocked_word": blocked}

    # Appel modèle
    try:
        reply = await call_model_for_level(lvl, token, level_id, user_message)
    except Exception as e:
        log.exception("LLM call failed for token=%s level=%s: %s", token, level_id, e)
        reply = "[ERREUR MODELE] Le modèle n'a pas répondu. Réessaie dans un instant."
    assistant_msg = db.add_message(token, level_id, "assistant", reply)
    await broadcast_admin(
        "message",
        token=token,
        level_id=level_id,
        message=assistant_msg,
    )
    return {"reply": reply}


async def _handle_verify(token: str, level_id: int, payload: dict, demo: bool) -> dict:
    guess = _safe_str(payload.get("guess"), 64).strip()
    if not guess:
        raise HTTPException(400, "Aucune saisie.")
    lvl = get_level(level_id)
    if not lvl:
        raise HTTPException(404, "Niveau inconnu.")
    check_progression(token, level_id, allow_demo_skip=demo)
    progress = ensure_level_secret(token, level_id)
    secret = progress["secret"]
    db.touch_session(token)

    if db.is_level_solved(token, level_id):
        return {"correct": True, "secret_format_hint": None}

    correct = hmac.compare_digest(guess.upper(), secret.upper())
    db.add_attempt(token, level_id, guess, correct)
    db.increment_attempt(token, level_id)
    await broadcast_admin(
        "attempt",
        token=token,
        level_id=level_id,
        guess=guess,
        correct=correct,
    )
    if correct:
        db.mark_solved(token, level_id)
        await broadcast_admin("solved", token=token, level_id=level_id)
    return {"correct": correct, "secret_format_hint": "MOT-XXXX" if not correct else None}


# ============================================================
#  Application ADMIN (port 8001)
# ============================================================

admin_app = FastAPI(title="IA'cking : Admin")
_setup_app(admin_app)


@admin_app.get("/health")
async def admin_health():
    return {"ok": True}


@admin_app.get("/")
async def admin_index():
    if not SERVE_STATIC:
        raise HTTPException(404, "Frontend non monté côté backend (servi par nginx).")
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))


@admin_app.get("/scoreboard")
async def admin_scoreboard_page(request: Request):
    """Page HTML de scoreboard. Si pas authentifié, redirige vers la page d'admin."""
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw or not read_admin_cookie(raw):
        return RedirectResponse(url="/", status_code=302)
    if not SERVE_STATIC:
        raise HTTPException(404, "Frontend non monté côté backend (servi par nginx).")
    return FileResponse(os.path.join(FRONTEND_DIR, "scoreboard.html"))


if SERVE_STATIC:
    admin_app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@admin_app.post("/admin/login")
@limiter.limit(ADMIN_LOGIN_RATE_LIMIT) 
async def admin_login(request: Request, payload: dict, response: Response):
    pwd = _safe_str(payload.get("password"), 256)
    if not hmac.compare_digest(pwd, ADMIN_PASSWORD):
        audit.warning("admin_login_failed ip=%s", client_ip(request))
        raise HTTPException(401, "Mot de passe incorrect.")
    response.set_cookie(ADMIN_COOKIE, make_admin_cookie(), **COOKIE_KW_ADMIN)
    audit.info("admin_login_success ip=%s", client_ip(request))
    return {"ok": True}


@admin_app.post("/admin/logout")
async def admin_logout(response: Response):
    # Efface la version actuelle (Domain=COOKIE_DOMAIN si défini) ET la
    # version host-only héritée des déploiements précédents, sans quoi
    # le navigateur conserve l'ancien cookie et la session admin persiste.
    response.delete_cookie(ADMIN_COOKIE, path="/", samesite="strict", domain=COOKIE_DOMAIN)
    if COOKIE_DOMAIN:
        response.delete_cookie(ADMIN_COOKIE, path="/", samesite="strict")
    return {"ok": True}


@admin_app.get("/admin/me")
async def admin_me(request: Request):
    require_admin(request)
    return {"ok": True}


@admin_app.post("/admin/tokens")
async def admin_create_token(request: Request):
    require_admin(request)
    token = db.create_session(is_demo=False)
    sess = db.get_session(token)
    audit.info("token_created token=%s by_ip=%s", token, client_ip(request))
    payload = {
        "token": token,
        "session": dict(sess) if sess else None,
    }
    await broadcast_admin("session_created", **payload)
    return payload


@admin_app.delete("/admin/tokens/{token}")
async def admin_revoke_token(request: Request, token: str):
    require_admin(request)
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(404, "Token inconnu.")
    db.revoke_session(token)
    audit.info("token_revoked token=%s by_ip=%s", token, client_ip(request))
    await broadcast_admin("session_revoked", token=token)
    return {"ok": True}


@admin_app.post("/admin/tokens/{token}/restore")
async def admin_restore_token(request: Request, token: str):
    require_admin(request)
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(404, "Token inconnu.")
    db.unrevoke_session(token)
    audit.info("token_restored token=%s by_ip=%s", token, client_ip(request))
    await broadcast_admin("session_restored", token=token)
    return {"ok": True}


@admin_app.delete("/admin/sessions/revoked")
async def admin_clean_revoked(request: Request):
    """Supprime toutes les sessions révoquées et leur historique"""
    require_admin(request)
    deleted = db.delete_revoked_sessions()
    audit.warning(
        "sessions_cleaned count=%d tokens=%s by_ip=%s",
        len(deleted), deleted, client_ip(request),
    )
    await broadcast_admin("sessions_cleaned", tokens=deleted)
    return {"deleted": len(deleted), "tokens": deleted}


@admin_app.delete("/admin/sessions/{token}")
async def admin_delete_session(request: Request, token: str):
    """Suppression d'une session unique et son historique"""
    require_admin(request)
    if token == db.DEMO_TOKEN:
        raise HTTPException(400, "Impossible de supprimer la session démo (utiliser le reset).")
    ok = db.delete_session(token)
    if not ok:
        raise HTTPException(404, "Token inconnu.")
    audit.warning("session_deleted token=%s by_ip=%s", token, client_ip(request))
    await broadcast_admin("sessions_cleaned", tokens=[token])
    return {"ok": True, "token": token}


@admin_app.get("/admin/scoreboard")
async def admin_scoreboard_data(request: Request):
    """Données JSON du scoreboard"""
    require_admin(request)
    bonus_ids = {lvl["id"] for lvl in LEVELS if lvl.get("bonus")}
    regular_levels = [
        {"id": lvl["id"], "name": lvl["name"]}
        for lvl in LEVELS if lvl["id"] not in bonus_ids
    ]
    bonus_levels = [
        {"id": lvl["id"], "name": lvl["name"]}
        for lvl in LEVELS if lvl["id"] in bonus_ids
    ]
    return {
        "scoreboard": db.compute_scoreboard(len(LEVELS), bonus_ids),
        "levels_total": len(LEVELS),
        "regular_levels": regular_levels,
        "bonus_levels": bonus_levels,
        "updated_at": db.now(),
    }


@admin_app.get("/admin/sessions")
async def admin_sessions(request: Request):
    require_admin(request)
    students = db.list_sessions(include_revoked=True, demo=False)
    demo = db.list_sessions(include_revoked=True, demo=True)
    out = {"students": [], "demo": []}
    for sess in students:
        sess["progress"] = db.list_progress(sess["token"])
        out["students"].append(sess)
    for sess in demo:
        sess["progress"] = db.list_progress(sess["token"])
        out["demo"].append(sess)
    return out


@admin_app.get("/admin/sessions/{token}/messages")
async def admin_messages(request: Request, token: str, level: int):
    require_admin(request)
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(404, "Token inconnu.")
    progress = db.get_progress(token, level)
    return {
        "messages": db.list_messages(token, level),
        "secret": progress["secret"] if progress else None,
        "solved": bool(progress and progress["solved"]),
        "attempts_log": db.list_attempts(token, level),
    }


@admin_app.post("/admin/sessions/{token}/hint")
async def admin_send_hint(request: Request, token: str, payload: dict):
    require_admin(request)
    level_id = payload.get("level_id")
    text = (payload.get("text") or "").strip()
    if not isinstance(level_id, int) or not get_level(level_id):
        raise HTTPException(400, "Niveau invalide.")
    if not text:
        raise HTTPException(400, "Indice vide.")
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(404, "Token inconnu.")
    ensure_level_secret(token, level_id)
    msg = db.add_message(token, level_id, "hint", text)
    db.increment_hints(token, level_id)
    await student_ws.send(token, {"event": "hint", "level_id": level_id, "message": msg})
    await broadcast_admin("message", token=token, level_id=level_id, message=msg)
    return {"ok": True, "message": msg}


@admin_app.post("/admin/demo/reset")
async def admin_reset_demo(request: Request):
    require_admin(request)
    db.reset_demo()
    await broadcast_admin("demo_reset")
    return {"ok": True}


@admin_app.post("/admin/help/{token}/acquit")
async def admin_acquit_help(request: Request, token: str):
    """Acquitte une demande d'aide : avertit l'élève (son bouton se réarme
    immédiatement) et resynchronise les autres dashboards admin."""
    require_admin(request)
    await broadcast_admin("help_acquitted", token=token)
    await student_ws.send(token, {"event": "help_acquitted"})
    return {"ok": True}


@admin_app.post("/admin/sessions/{token}/levels/{level_id}/reset")
async def admin_reset_level(request: Request, token: str, level_id: int):
    """Régénère le secret + efface l'historique pour ce (token × level)."""
    require_admin(request)
    sess = db.get_session(token)
    if not sess:
        raise HTTPException(404, "Token inconnu.")
    if not get_level(level_id):
        raise HTTPException(404, "Niveau inconnu.")
    db.reset_level(token, level_id)
    await broadcast_admin("level_reset", token=token, level_id=level_id)
    await student_ws.send(
        token,
        {"event": "level_reset", "level_id": level_id},
    )
    return {"ok": True}


@admin_app.get("/admin/levels")
async def admin_levels(request: Request):
    """Méta sur les niveaux avec modèle"""
    require_admin(request)
    return {
        "levels": [
            {
                **public_level_info(lvl),
                "model": lvl["model"],
                "hint": lvl["hint"],
                "input_banned": lvl["input_banned"],
                "output_filter_secret": lvl["output_filter_secret"],
            }
            for lvl in LEVELS
        ]
    }


@admin_app.get("/admin/cheatsheet")
async def admin_cheatsheet(request: Request):
    """Renvoie la cheatsheet markdown brute. Auth admin obligatoire."""
    require_admin(request)
    default = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "CHEATSHEET.md"))
    path = os.getenv("CHEATSHEET_PATH", default)
    if not os.path.exists(path):
        raise HTTPException(404, "CHEATSHEET.md introuvable.")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


def _csv_safe(value) -> str:
    """Neutralise l'injection de formules CSV (OWASP). Les contenus élève
    (prénom, messages, tentatives) sont arbitraires : une cellule commençant
    par = + - @ (ou tab/CR) serait interprétée comme formule par Excel/
    LibreOffice à l'ouverture de l'export. On préfixe par une apostrophe."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


@admin_app.get("/admin/export.csv")
async def admin_export(request: Request):
    require_admin(request)
    data = db.export_all()
    out = io.StringIO()
    w = csv.writer(out)

    def row(values):
        w.writerow([_csv_safe(v) for v in values])

    row(["# SESSIONS"])
    row(["token", "name", "created_at", "last_activity", "revoked", "is_demo"])
    for s in data["sessions"]:
        row([s["token"], s["name"], s["created_at"], s["last_activity"], s["revoked"], s["is_demo"]])

    w.writerow([])
    row(["# PROGRESS"])
    row(["token", "level_id", "secret", "solved", "attempts", "hints_received", "started_at", "solved_at"])
    for p in data["progress"]:
        row([p["session_token"], p["level_id"], p["secret"], p["solved"], p["attempts"], p["hints_received"], p["started_at"], p["solved_at"]])

    w.writerow([])
    row(["# MESSAGES"])
    row(["id", "token", "level_id", "role", "content", "created_at"])
    for m in data["messages"]:
        row([m["id"], m["session_token"], m["level_id"], m["role"], m["content"], m["created_at"]])

    w.writerow([])
    row(["# ATTEMPTS"])
    row(["id", "token", "level_id", "guess", "correct", "created_at"])
    for a in data["attempts"]:
        row([a["id"], a["session_token"], a["level_id"], a["guess"], a["correct"], a["created_at"]])

    csv_bytes = out.getvalue().encode("utf-8")
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="workshop_export.csv"'},
    )


@admin_app.websocket("/admin/ws/monitor")
async def admin_websocket(ws: WebSocket):
    if not _ws_origin_allowed(ws):
        await ws.close(code=1008)
        return
    raw = ws.cookies.get(ADMIN_COOKIE)
    if not raw or not read_admin_cookie(raw):
        await ws.close(code=1008)
        return
    if admin_ws.count() >= WS_MAX_PER_TOKEN * 2:
        await ws.close(code=1013)
        return
    await admin_ws.connect(ws)
    try:
        await _ws_keepalive_loop(ws)
    except WebSocketDisconnect:
        pass
    finally:
        await admin_ws.disconnect(ws)


# ============================================================
#  Lancement uvicorn (deux serveurs en parallèle)
# ============================================================

async def _serve() -> None:
    import uvicorn
    config_student = uvicorn.Config(
        student_app, host=HOST, port=STUDENT_PORT, log_level="info", access_log=False,
    )
    config_admin = uvicorn.Config(
        admin_app, host=HOST, port=ADMIN_PORT, log_level="info", access_log=False,
    )
    server_student = uvicorn.Server(config_student)
    server_admin = uvicorn.Server(config_admin)
    await asyncio.gather(server_student.serve(), server_admin.serve())


def main() -> None:
    fail_fast()
    db.init_db()
    log.info("== IA'cking ==")
    log.info("  Élève : http://%s:%s/", HOST, STUDENT_PORT)
    log.info("  Démo  : http://%s:%s/demo", HOST, STUDENT_PORT)
    log.info("  Admin : http://%s:%s/", HOST, ADMIN_PORT)
    log.info("  DB    : %s", db.DB_PATH)
    log.info("  SECURE_COOKIES=%s  HOST=%s", SECURE_COOKIES, HOST)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
