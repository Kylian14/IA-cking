"""Gestion des connexions WebSocket.
Deux managers indépendants :
- `student_ws` : un élève par token, recevoir les indices push depuis l'admin
- `admin_ws` : les dashboards admin connectés, broadcast sur chaque event
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from fastapi import WebSocket


class StudentWS:
    """Une connexion par token"""

    def __init__(self) -> None:
        self._conns: Dict[str, List[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, token: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.setdefault(token, []).append(ws)

    async def disconnect(self, token: str, ws: WebSocket) -> None:
        async with self._lock:
            if token in self._conns:
                self._conns[token] = [w for w in self._conns[token] if w is not ws]
                if not self._conns[token]:
                    del self._conns[token]

    async def send(self, token: str, payload: Dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self._conns.get(token, []))
        for ws in sockets:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                pass

    def count(self, token: str) -> int:
        return len(self._conns.get(token, []))


class AdminWS:
    """Tous les dashboards admin connectés reçoivent chaque broadcast"""
    def __init__(self) -> None:
        self._conns: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns = [w for w in self._conns if w is not ws]

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self._conns)
        text = json.dumps(payload)
        for ws in sockets:
            try:
                await ws.send_text(text)
            except Exception:
                pass

    def count(self) -> int:
        return len(self._conns)


student_ws = StudentWS()
admin_ws = AdminWS()
