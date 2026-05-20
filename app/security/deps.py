# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.models import Users


def login_required(request: Request, db: Session) -> Users:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Vui lòng đăng nhập.")

    user = db.get(Users, int(user_id))
    if not user or int(user.is_active or 0) != 1:
        raise HTTPException(status_code=401, detail="Vui lòng đăng nhập.")

    return user