# -*- coding: utf-8 -*-
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, case
from sqlalchemy.orm import column_property, relationship

from .database import Base


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    LOCKED = "LOCKED"


class Users(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    unit_code = Column(String, nullable=False, default="THANH_VIEN_TRUNG_TAP")
    role_code = Column(String, nullable=False, default="THANH_VIEN_TRUNG_TAP")
    position_title = Column(String, nullable=False, default="Thành viên trưng tập")
    is_active = Column(Integer, nullable=False, default=1)
    created_at = Column(String, nullable=False)

    status = column_property(
        case(
            (is_active == 1, "ACTIVE"),
            else_="LOCKED",
        )
    )


class Units(Base):
    __tablename__ = "units"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ten_don_vi = Column(String, nullable=False)
    cap_do = Column(Integer, nullable=False, default=1)
    parent_id = Column(String, ForeignKey("units.id"), nullable=True)
    path = Column(String, nullable=True)
    trang_thai = Column(String, nullable=False, default="ACTIVE")
    order_index = Column(Integer, nullable=False, default=0)

    parent = relationship("Units", remote_side=[id], backref="children")


class UserUnitMemberships(Base):
    __tablename__ = "user_unit_memberships"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    unit_id = Column(String, ForeignKey("units.id"), nullable=False)
    is_primary = Column(Boolean, default=True)

    user = relationship("Users", foreign_keys=[user_id])
    unit = relationship("Units", foreign_keys=[unit_id])

    __table_args__ = (
        UniqueConstraint("user_id", "unit_id", name="uq_user_unit"),
    )


class Roles(Base):
    __tablename__ = "roles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)


class UserRoles(Base):
    __tablename__ = "user_roles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role_id = Column(String, ForeignKey("roles.id"), nullable=False)
    scope_code = Column(String, nullable=True)

    user = relationship("Users", foreign_keys=[user_id])
    role = relationship("Roles", foreign_keys=[role_id])

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )


class Tasks(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Committees(Base):
    __tablename__ = "committees"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    

class DocumentDrafts(Base):
    __tablename__ = "document_drafts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=False)
    document_type = Column(String, nullable=True)
    summary = Column(Text, nullable=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_unit_code = Column(String, nullable=True, index=True)

    current_status = Column(String, nullable=False, default="DRAFT", index=True)
    current_handler_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    current_handler_unit_code = Column(String, nullable=True, index=True)
    current_role_code = Column(String, nullable=True, index=True)

    last_submitter_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    current_file_id = Column(String, nullable=True, index=True)

    is_deleted = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    creator = relationship("Users", foreign_keys=[created_by])
    current_handler = relationship("Users", foreign_keys=[current_handler_user_id])
    last_submitter = relationship("Users", foreign_keys=[last_submitter_id])

    files = relationship(
        "DocumentDraftFiles",
        back_populates="draft",
        cascade="all, delete-orphan",
        foreign_keys="DocumentDraftFiles.draft_id",
    )

    actions = relationship(
        "DocumentDraftActions",
        back_populates="draft",
        cascade="all, delete-orphan",
        foreign_keys="DocumentDraftActions.draft_id",
    )


class DocumentDraftFiles(Base):
    __tablename__ = "document_draft_files"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id = Column(String, ForeignKey("document_drafts.id"), nullable=False, index=True)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    file_role = Column(String, nullable=False, default="DRAFT_UPLOAD", index=True)
    file_name = Column(String, nullable=False)
    stored_name = Column(String, nullable=True)
    file_path = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0)

    is_active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)

    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    draft = relationship("DocumentDrafts", back_populates="files", foreign_keys=[draft_id])
    uploaded_by = relationship("Users", foreign_keys=[uploaded_by_user_id])


class DocumentDraftActions(Base):
    __tablename__ = "document_draft_actions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id = Column(String, ForeignKey("document_drafts.id"), nullable=False, index=True)

    action_type = Column(String, nullable=False, index=True)

    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    from_unit_code = Column(String, nullable=True)
    to_unit_code = Column(String, nullable=True)

    comment = Column(Text, nullable=True)

    linked_file_id = Column(String, ForeignKey("document_draft_files.id"), nullable=True, index=True)

    is_pending = Column(Boolean, nullable=False, default=False)
    response_text = Column(Text, nullable=True)
    responded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    draft = relationship("DocumentDrafts", back_populates="actions", foreign_keys=[draft_id])
    from_user = relationship("Users", foreign_keys=[from_user_id])
    to_user = relationship("Users", foreign_keys=[to_user_id])
    linked_file = relationship("DocumentDraftFiles", foreign_keys=[linked_file_id])    