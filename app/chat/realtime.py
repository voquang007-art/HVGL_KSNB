# -*- coding: utf-8 -*-
"""
app/chat/realtime.py

Khung realtime dùng chung cho hệ thống.
- Giữ nguyên group socket của chat
- Giữ nguyên notify socket theo user
- Bổ sung helper gửi JSON để các module khác (work/task/inbox...) dùng chung
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, Set

from fastapi import WebSocket


class ChatConnectionManager:
    def __init__(self) -> None:
        self.group_connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self.notify_connections: Dict[str, Set[WebSocket]] = defaultdict(set)

    async def connect_group(self, group_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.group_connections[group_id].add(websocket)

    def disconnect_group(self, group_id: str, websocket: WebSocket) -> None:
        if group_id in self.group_connections:
            self.group_connections[group_id].discard(websocket)
            if not self.group_connections[group_id]:
                self.group_connections.pop(group_id, None)

    async def connect_notify(self, user_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.notify_connections[user_id].add(websocket)

    def disconnect_notify(self, user_id: str, websocket: WebSocket) -> None:
        if user_id in self.notify_connections:
            self.notify_connections[user_id].discard(websocket)
            if not self.notify_connections[user_id]:
                self.notify_connections.pop(user_id, None)

    async def broadcast_group_text(self, group_id: str, message: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.group_connections.get(group_id, set())):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect_group(group_id, ws)

    async def notify_user_text(self, user_id: str, message: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.notify_connections.get(user_id, set())):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect_notify(user_id, ws)

    async def notify_user_json(self, user_id: str, payload: Dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.notify_connections.get(user_id, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect_notify(user_id, ws)

    async def notify_users_json(self, user_ids: Iterable[str], payload: Dict[str, Any]) -> None:
        sent_to: Set[str] = set()
        for raw_user_id in user_ids:
            user_id = str(raw_user_id or "").strip()
            if not user_id or user_id in sent_to:
                continue
            sent_to.add(user_id)
            await self.notify_user_json(user_id, payload)


manager = ChatConnectionManager()