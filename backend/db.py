"""Couche SQLite
Quatre tables : sessions, progress, messages, attempts.
Utilise sqlite3 standard, une connexion par appel
"""

from __future__ import annotations

import sqlite3
import os
import secrets as pysecrets
import string
import time
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.getenv("DB_PATH") or os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "workshop.db")
)
DEMO_TOKEN = "DEMO"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    name TEXT,
    created_at REAL NOT NULL,
    last_activity REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS progress (
    session_token TEXT NOT NULL,
    level_id INTEGER NOT NULL,
    secret TEXT NOT NULL,
    solved INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    hints_received INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    solved_at REAL,
    PRIMARY KEY (session_token, level_id),
    FOREIGN KEY (session_token) REFERENCES sessions(token) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL,
    level_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','hint','system_note')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_token) REFERENCES sessions(token) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_level
    ON messages(session_token, level_id, created_at);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL,
    level_id INTEGER NOT NULL,
    guess TEXT NOT NULL,
    correct INTEGER NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_token) REFERENCES sessions(token) ON DELETE CASCADE
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA busy_timeout = 5000")
    c.execute("PRAGMA synchronous = NORMAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


MAX_MESSAGES_PER_LEVEL = int(os.getenv("MAX_MESSAGES_PER_LEVEL", "500"))


def init_db() -> None:
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
    ensure_demo_session()


def now() -> float:
    return time.time()


# ---------- Sessions ----------

def generate_token(existing) -> str:
    while True:
        t = "".join(pysecrets.choice(string.digits) for _ in range(6))
        if t not in existing and t != DEMO_TOKEN:
            return t


def list_active_tokens() -> set[str]:
    with conn() as c:
        rows = c.execute("SELECT token FROM sessions WHERE revoked = 0").fetchall()
        return {r["token"] for r in rows}


def create_session(is_demo: bool = False, token: Optional[str] = None) -> str:
    if is_demo:
        token = DEMO_TOKEN
    forced = token is not None
    for _ in range(20):
        if not forced:
            token = generate_token(list_active_tokens())
        t = now()
        try:
            with conn() as c:
                c.execute(
                    "INSERT INTO sessions (token, name, created_at, last_activity, revoked, is_demo) "
                    "VALUES (?, NULL, ?, ?, 0, ?)",
                    (token, t, t, 1 if is_demo else 0),
                )
            return token
        except sqlite3.IntegrityError:
            if forced:
                raise
            continue
    raise RuntimeError("Impossible de générer un token unique après 20 essais.")


def ensure_demo_session() -> None:
    with conn() as c:
        row = c.execute("SELECT token FROM sessions WHERE token = ?", (DEMO_TOKEN,)).fetchone()
    if row is None:
        create_session(is_demo=True)


def get_session(token: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()


def set_session_name(token: str, name: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE sessions SET name = COALESCE(name, ?), last_activity = ? WHERE token = ?",
            (name, now(), token),
        )


def touch_session(token: str) -> None:
    with conn() as c:
        c.execute("UPDATE sessions SET last_activity = ? WHERE token = ?", (now(), token))


def revoke_session(token: str) -> None:
    with conn() as c:
        c.execute("UPDATE sessions SET revoked = 1 WHERE token = ?", (token,))


def unrevoke_session(token: str) -> None:
    with conn() as c:
        c.execute("UPDATE sessions SET revoked = 0 WHERE token = ?", (token,))


def delete_revoked_sessions() -> list[str]:
    """Supprime définitivement les sessions révoquées et tout
    leur historique (messages, tentatives, progression). Renvoie les tokens
    supprimés pour notification WebSocket."""
    with conn() as c:
        rows = c.execute(
            "SELECT token FROM sessions WHERE revoked = 1 AND is_demo = 0"
        ).fetchall()
        tokens = [r["token"] for r in rows]
        if not tokens:
            return []
        # `placeholders` n'est qu'une suite de "?" (un par token) ; les valeurs
        # sont passées en paramètres liés -> pas d'injection. nosec : B608 ne
        # voit que l'interpolation de la chaîne, pas qu'elle ne contient que "?".
        placeholders = ",".join("?" * len(tokens))
        c.execute(f"DELETE FROM messages WHERE session_token IN ({placeholders})", tokens)   # nosec B608
        c.execute(f"DELETE FROM attempts WHERE session_token IN ({placeholders})", tokens)   # nosec B608
        c.execute(f"DELETE FROM progress WHERE session_token IN ({placeholders})", tokens)   # nosec B608
        c.execute(f"DELETE FROM sessions WHERE token IN ({placeholders})", tokens)           # nosec B608
    return tokens


def compute_scoreboard(levels_total: int, bonus_ids: Optional[set[int]] = None) -> list[dict]:
    """Classement des sessions élèves (hors démo).

    Les niveaux dont l'id est dans `bonus_ids` sont **exclus du classement
    et du temps global** ; ils sont remontés à part dans `bonus`.

    Tri : nb niveaux non-bonus résolus DESC, puis nb messages chat
    (role=user) non-bonus ASC (l'élève efficace utilise peu de messages),
    puis tentatives de validation ASC, puis hints ASC, puis date
    dernière résolution ASC. Le temps total reste calculé pour info
    mais ne sert plus au tri.
    """
    bonus_ids = bonus_ids or set()
    with conn() as c:
        sessions = c.execute(
            "SELECT token, name, created_at, last_activity, revoked "
            "FROM sessions WHERE is_demo = 0"
        ).fetchall()
        # Nombre de messages user par (token, level_id) en un coup pour éviter
        # un round-trip par session.
        msg_rows = c.execute(
            "SELECT session_token, level_id, COUNT(*) AS n "
            "FROM messages WHERE role = 'user' "
            "GROUP BY session_token, level_id"
        ).fetchall()
        msg_count: dict[tuple[str, int], int] = {
            (r["session_token"], r["level_id"]): r["n"] for r in msg_rows
        }
        result = []
        for s in sessions:
            token = s["token"]
            progress = c.execute(
                "SELECT level_id, solved, attempts, hints_received, started_at, solved_at "
                "FROM progress WHERE session_token = ?",
                (token,),
            ).fetchall()

            def _time(p) -> Optional[int]:
                if p["solved_at"] and p["started_at"]:
                    return int(p["solved_at"] - p["started_at"])
                return None

            # Temps par niveau
            level_times: dict[int, int] = {}
            for p in progress:
                if p["solved"] and (t := _time(p)) is not None:
                    level_times[p["level_id"]] = t

            # Sépare régulier et bonus
            regular = [p for p in progress if p["level_id"] not in bonus_ids]
            bonus = [p for p in progress if p["level_id"] in bonus_ids]

            reg_solved = [p for p in regular if p["solved"]]
            bon_solved = [p for p in bonus if p["solved"]]

            solved_ids = sorted(p["level_id"] for p in reg_solved)
            bonus_solved_ids = sorted(p["level_id"] for p in bon_solved)

            total_time = sum(_time(p) or 0 for p in reg_solved)
            bonus_total_time = sum(_time(p) or 0 for p in bon_solved)

            total_attempts = sum(p["attempts"] for p in regular)
            total_hints = sum(p["hints_received"] for p in regular)
            # Compte les messages chat (role=user) envoyés au LLM.
            # Exclut les niveaux bonus du total principal (cohérence avec
            # le reste : le bonus n'entre pas dans le score officiel).
            total_messages = sum(
                msg_count.get((token, p["level_id"]), 0) for p in regular
            )
            bonus_total_messages = sum(
                msg_count.get((token, p["level_id"]), 0) for p in bonus
            )

            last_solved_at = max(
                (p["solved_at"] for p in reg_solved if p["solved_at"]), default=None
            )
            last_solved_level = next(
                (p["level_id"] for p in reg_solved if p["solved_at"] == last_solved_at),
                None,
            ) if last_solved_at else None

            current_level = (max(solved_ids) + 1) if solved_ids else 1
            while current_level in bonus_ids:
                current_level += 1
            regular_ids = [lvl for lvl in range(1, levels_total + 1) if lvl not in bonus_ids]
            if not regular_ids or current_level > max(regular_ids):
                current_level = None

            result.append({
                "token": token,
                "name": s["name"] or "?",
                "revoked": bool(s["revoked"]),
                "solved_count": len(reg_solved),
                "solved_levels": solved_ids,
                "level_times": level_times,
                "total_attempts": total_attempts,
                "total_hints": total_hints,
                "total_messages": total_messages,
                "total_time_sec": total_time,
                "last_solved_level": last_solved_level,
                "last_solved_at": last_solved_at,
                "current_level": current_level,
                "last_activity": s["last_activity"],
                "bonus": {
                    "solved_count": len(bon_solved),
                    "solved_levels": bonus_solved_ids,
                    "total_time_sec": bonus_total_time,
                    "total_messages": bonus_total_messages,
                    "attempts": sum(p["attempts"] for p in bonus),
                    "hints": sum(p["hints_received"] for p in bonus),
                },
            })
    # Tri : niveaux résolus DESC, puis nb messages chat ASC (efficacité),
    # puis tentatives de validation ASC, puis hints ASC, puis date ASC.
    # Le temps n'est plus discriminant.
    result.sort(key=lambda r: (
        -r["solved_count"],
        r["total_messages"],
        r["total_attempts"],
        r["total_hints"],
        r["last_solved_at"] or 1e18,
    ))
    for i, r in enumerate(result, 1):
        r["rank"] = i
    return result


def delete_session(token: str) -> bool:
    """Suppression définitive d'une session avec son
    historique complet"""
    with conn() as c:
        existed = c.execute("SELECT 1 FROM sessions WHERE token = ?", (token,)).fetchone()
        if not existed:
            return False
        c.execute("DELETE FROM messages WHERE session_token = ?", (token,))
        c.execute("DELETE FROM attempts WHERE session_token = ?", (token,))
        c.execute("DELETE FROM progress WHERE session_token = ?", (token,))
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return True


def list_sessions(include_revoked: bool = True, demo: bool = False):
    q = "SELECT * FROM sessions WHERE is_demo = ?"
    args: list = [1 if demo else 0]
    if not include_revoked:
        q += " AND revoked = 0"
    q += " ORDER BY created_at DESC"
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


# ---------- Progress ----------

def get_progress(token: str, level_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM progress WHERE session_token = ? AND level_id = ?",
            (token, level_id),
        ).fetchone()


def list_progress(token: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM progress WHERE session_token = ? ORDER BY level_id",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


def ensure_progress(token: str, level_id: int, secret: str) -> sqlite3.Row:
    """Crée l'entrée progress si elle n'existe pas, sinon retourne l'existante."""
    existing = get_progress(token, level_id)
    if existing:
        return existing
    t = now()
    with conn() as c:
        c.execute(
            "INSERT INTO progress (session_token, level_id, secret, solved, attempts, hints_received, started_at) "
            "VALUES (?, ?, ?, 0, 0, 0, ?)",
            (token, level_id, secret, t),
        )
    return get_progress(token, level_id) 


def increment_attempt(token: str, level_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE progress SET attempts = attempts + 1 WHERE session_token = ? AND level_id = ?",
            (token, level_id),
        )


def increment_hints(token: str, level_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE progress SET hints_received = hints_received + 1 WHERE session_token = ? AND level_id = ?",
            (token, level_id),
        )


def mark_solved(token: str, level_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE progress SET solved = 1, solved_at = ? WHERE session_token = ? AND level_id = ?",
            (now(), token, level_id),
        )


def is_level_solved(token: str, level_id: int) -> bool:
    p = get_progress(token, level_id)
    return bool(p and p["solved"])


def reset_demo() -> None:
    """Efface tout l'historique de la session démo et regénère."""
    with conn() as c:
        c.execute("DELETE FROM messages WHERE session_token = ?", (DEMO_TOKEN,))
        c.execute("DELETE FROM attempts WHERE session_token = ?", (DEMO_TOKEN,))
        c.execute("DELETE FROM progress WHERE session_token = ?", (DEMO_TOKEN,))
        c.execute("UPDATE sessions SET last_activity = ? WHERE token = ?", (now(), DEMO_TOKEN))


def reset_level(token: str, level_id: int) -> None:
    """Réinitialise un niveau pour une session :
    - efface tous les messages et tentatives de ce niveau
    - supprime la progression
    """
    with conn() as c:
        c.execute(
            "DELETE FROM messages WHERE session_token = ? AND level_id = ?",
            (token, level_id),
        )
        c.execute(
            "DELETE FROM attempts WHERE session_token = ? AND level_id = ?",
            (token, level_id),
        )
        c.execute(
            "DELETE FROM progress WHERE session_token = ? AND level_id = ?",
            (token, level_id),
        )
        c.execute("UPDATE sessions SET last_activity = ? WHERE token = ?", (now(), token))


# ---------- Messages ----------

def add_message(token: str, level_id: int, role: str, content: str) -> dict:
    t = now()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO messages (session_token, level_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, level_id, role, content, t),
        )
        msg_id = cur.lastrowid
        if MAX_MESSAGES_PER_LEVEL > 0:
            c.execute(
                "DELETE FROM messages WHERE id IN ("
                "  SELECT id FROM messages "
                "  WHERE session_token = ? AND level_id = ? "
                "  ORDER BY created_at DESC, id DESC "
                "  LIMIT -1 OFFSET ?"
                ")",
                (token, level_id, MAX_MESSAGES_PER_LEVEL),
            )
    return {
        "id": msg_id,
        "session_token": token,
        "level_id": level_id,
        "role": role,
        "content": content,
        "created_at": t,
    }


def count_llm_calls(token: str) -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE session_token = ? AND role = 'assistant' "
            "AND content NOT LIKE '[FILTRE%' AND content NOT LIKE '[ERREUR MODELE]%'",
            (token,),
        ).fetchone()
        return int(row["n"]) if row else 0


def list_messages(token: str, level_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE session_token = ? AND level_id = ? ORDER BY created_at, id",
            (token, level_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_chat_history_for_groq(token: str, level_id: int, max_pairs: int = 10) -> list[dict]:
    """Retourne au plus `max_pairs` paires (user, assistant) en ordre chronologique"""
    if max_pairs <= 0:
        return []
    fetch_limit = 2 * max_pairs + 2
    with conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages "
            "WHERE session_token = ? AND level_id = ? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (token, level_id, fetch_limit),
        ).fetchall()
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    pairs: list[tuple[dict, dict]] = []
    i = 0
    while i + 1 < len(msgs):
        if msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "assistant":
            pairs.append((msgs[i], msgs[i + 1]))
            i += 2
        else:
            i += 1
    pairs = pairs[-max_pairs:]
    out: list[dict] = []
    for u, a in pairs:
        out.append(u)
        out.append(a)
    return out


# ---------- Attempts ----------

def add_attempt(token: str, level_id: int, guess: str, correct: bool) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO attempts (session_token, level_id, guess, correct, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, level_id, guess, 1 if correct else 0, now()),
        )


def list_attempts(token: str, level_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM attempts WHERE session_token = ? AND level_id = ? ORDER BY created_at",
            (token, level_id),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Export ----------

def export_all() -> dict:
    """Dump complet pour l'export CSV/JSON."""
    with conn() as c:
        sessions = [dict(r) for r in c.execute("SELECT * FROM sessions").fetchall()]
        progress = [dict(r) for r in c.execute("SELECT * FROM progress").fetchall()]
        messages = [dict(r) for r in c.execute("SELECT * FROM messages").fetchall()]
        attempts = [dict(r) for r in c.execute("SELECT * FROM attempts").fetchall()]
    return {
        "sessions": sessions,
        "progress": progress,
        "messages": messages,
        "attempts": attempts,
    }
