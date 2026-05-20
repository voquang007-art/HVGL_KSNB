# -*- coding: utf-8 -*-
"""
app/chat/deps.py

Helper riêng cho module chat.
Giai đoạn 1 chỉ giữ mức tối thiểu.
"""

from __future__ import annotations


def get_display_name(user) -> str:
    if user is None:
        return "Người dùng"

    full_name = (getattr(user, "full_name", None) or "").strip()
    username = (getattr(user, "username", None) or "").strip()

    if full_name:
        return full_name
    if username:
        return username
    return "Người dùng"


def get_user_initials(user) -> str:
    name = get_display_name(user).strip()
    if not name:
        return "U"

    parts = [p for p in name.split() if p]
    if not parts:
        return "U"

    if len(parts) == 1:
        return parts[0][:1].upper()

    return (parts[0][:1] + parts[-1][:1]).upper()
