# -*- coding: utf-8 -*-
"""
app/chat/models.py

Bộ model chat cho giai đoạn 1.
Nguyên tắc:
- Không đụng schema cũ.
- FK người dùng trỏ về users.id của QLCV_App.
- Chỉ tạo khung dữ liệu chuẩn để giai đoạn 2 nối logic thật.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class ChatGroups(Base):
    __tablename__ = "chat_groups"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, index=True)

    owner_user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    # PRIVATE / SYSTEM / UNIT / TASK
    group_type = Column(String, nullable=False, default="PRIVATE", index=True)

    # Dự phòng cho giai đoạn sau
    unit_id = Column(String, ForeignKey("units.id"), nullable=True, index=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True, index=True)

    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner = relationship("Users", foreign_keys=[owner_user_id])
    unit = relationship("Units", foreign_keys=[unit_id])
    task = relationship("Tasks", foreign_keys=[task_id])

    members = relationship(
        "ChatGroupMembers",
        back_populates="group",
        cascade="all, delete-orphan",
    )
    messages = relationship(
        "ChatMessages",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class ChatGroupMembers(Base):
    __tablename__ = "chat_group_members"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String, ForeignKey("chat_groups.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    # owner / moderator / member
    member_role = Column(String, nullable=False, default="member")
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    is_muted = Column(Boolean, nullable=False, default=False)
    is_hidden = Column(Boolean, nullable=False, default=False)
    last_read_at = Column(DateTime, nullable=True)
    is_new_group = Column(Boolean, nullable=False, default=False)
    new_group_marked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_chat_group_user"),
    )

    group = relationship("ChatGroups", back_populates="members")
    user = relationship("Users", foreign_keys=[user_id])


class ChatMessages(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String, ForeignKey("chat_groups.id"), nullable=False, index=True)
    sender_user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    # TEXT / FILE / SYSTEM
    message_type = Column(String, nullable=False, default="TEXT")
    content = Column(Text, nullable=True)

    recalled = Column(Boolean, nullable=False, default=False)
    deleted_by_owner = Column(Boolean, nullable=False, default=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    pinned_at = Column(DateTime, nullable=True)
    pinned_by_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    reply_to_message_id = Column(String, ForeignKey("chat_messages.id"), nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    group = relationship("ChatGroups", back_populates="messages")
    sender = relationship("Users", foreign_keys=[sender_user_id])
    pinned_by = relationship("Users", foreign_keys=[pinned_by_user_id])
    reply_to = relationship("ChatMessages", remote_side=[id], foreign_keys=[reply_to_message_id])
    
    likes = relationship(
        "ChatMessageLikes",
        back_populates="message",
        cascade="all, delete-orphan",
    )
    attachments = relationship(
        "ChatAttachments",
        back_populates="message",
        cascade="all, delete-orphan",
    )


class ChatMessageLikes(Base):
    __tablename__ = "chat_message_likes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id = Column(String, ForeignKey("chat_messages.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    reaction_type = Column(String, nullable=False, default="like")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_chat_message_like"),
    )

    message = relationship("ChatMessages", back_populates="likes")
    user = relationship("Users", foreign_keys=[user_id])


class ChatAttachments(Base):
    __tablename__ = "chat_attachments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id = Column(String, ForeignKey("chat_messages.id"), nullable=False, index=True)

    filename = Column(String, nullable=False)
    stored_name = Column(String, nullable=True)
    path = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0)

    recalled = Column(Boolean, nullable=False, default=False)
    deleted_by_owner = Column(Boolean, nullable=False, default=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    pinned_at = Column(DateTime, nullable=True)
    pinned_by_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)

    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    message = relationship("ChatMessages", back_populates="attachments")
    pinned_by = relationship("Users", foreign_keys=[pinned_by_user_id])

class ChatMeetings(Base):
    __tablename__ = "chat_meetings"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String, ForeignKey("chat_groups.id"), nullable=False, index=True, unique=True)

    designed_by_user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    host_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    secretary_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)

    # TEAM / DEPARTMENT / HDTV / CROSS_DEPARTMENT / CROSS_TEAM / COMMITTEE
    meeting_scope = Column(String, nullable=False, default="TEAM", index=True)

    # Nếu meeting_scope = COMMITTEE thì lưu Ban kiêm nhiệm tương ứng
    committee_id = Column(String, ForeignKey("committees.id"), nullable=True, index=True)

    # UPCOMING / LIVE / ENDED
    meeting_status = Column(String, nullable=False, default="UPCOMING", index=True)

    scheduled_start_at = Column(DateTime, nullable=True, index=True)
    scheduled_end_at = Column(DateTime, nullable=True)
    agenda = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    group = relationship("ChatGroups", foreign_keys=[group_id])
    designed_by = relationship("Users", foreign_keys=[designed_by_user_id])
    host = relationship("Users", foreign_keys=[host_user_id])
    secretary = relationship("Users", foreign_keys=[secretary_user_id])
    committee = relationship("Committees", foreign_keys=[committee_id])


class ChatMeetingAttendances(Base):
    __tablename__ = "chat_meeting_attendances"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    meeting_id = Column(String, ForeignKey("chat_meetings.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    # PENDING / ABSENT / CHECKED_IN
    attendance_status = Column(String, nullable=False, default="PENDING", index=True)

    # OFFLINE / ONLINE
    presence_status = Column(String, nullable=False, default="OFFLINE", index=True)

    absent_reason = Column(Text, nullable=True)
    checked_in_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("meeting_id", "user_id", name="uq_chat_meeting_attendance_user"),
    )

    meeting = relationship("ChatMeetings", foreign_keys=[meeting_id])
    user = relationship("Users", foreign_keys=[user_id])


class ChatMeetingSpeakerRequests(Base):
    __tablename__ = "chat_meeting_speaker_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    meeting_id = Column(String, ForeignKey("chat_meetings.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    queue_no = Column(Integer, nullable=False, default=1)

    # PENDING / APPROVED / SPEAKING / DONE
    request_status = Column(String, nullable=False, default="PENDING", index=True)

    note = Column(Text, nullable=True)
    approved_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    meeting = relationship("ChatMeetings", foreign_keys=[meeting_id])
    user = relationship("Users", foreign_keys=[user_id])
    
class ChatMeetingLeaveRequests(Base):
    __tablename__ = "chat_meeting_leave_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    meeting_id = Column(String, ForeignKey("chat_meetings.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    # PENDING / APPROVED / REJECTED
    request_status = Column(String, nullable=False, default="PENDING", index=True)

    note = Column(Text, nullable=True)
    response_note = Column(Text, nullable=True)

    decided_by_user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    decided_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    meeting = relationship("ChatMeetings", foreign_keys=[meeting_id])
    user = relationship("Users", foreign_keys=[user_id])
    decided_by = relationship("Users", foreign_keys=[decided_by_user_id])    