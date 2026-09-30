import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Record(Base):
    """一条脱敏记录的当前状态；历史原文存放在 record_versions。"""

    __tablename__ = "records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 受控记录：发布必须走“申请-复核”流程，直接发布请求一律拒绝
    controlled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecordVersion(Base):
    """某次修订提交的原文与敏感区间，按 (record_id, version) 唯一。"""

    __tablename__ = "record_versions"

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    # JSONB 数组，元素为 {"start": int, "end": int}，左闭右开、按 Unicode 码点计
    intervals: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Snapshot(Base):
    """发布产生的不可变快照：只保存脱敏后的正文，后续修订不影响它。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("record_id", "version", name="uq_snapshots_record_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    redacted_text: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReadToken(Base):
    """快照的随机读取令牌，可设到期时间、可撤销。"""

    __tablename__ = "read_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshots.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 受控发布申请的终态
REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_STATUSES = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_REJECTED)


class PublishRequest(Base):
    """受控发布申请：绑定提交时刻的历史版本内容（原文与区间在此冻结）。

    - pending：待复核，可被批准或驳回
    - approved / rejected：终态，终态唯一、不可变更
    """

    __tablename__ = "publish_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_publish_requests_status"),
        Index("ix_publish_requests_record", "record_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    # 申请目标版本；待审内容冻结在 frozen_* 字段，后续修订不改变它
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_text: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_intervals: Mapped[list] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=REQUEST_PENDING, default=REQUEST_PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 令牌到期策略（提交时确定）：相对时长在批准签发时才换算成绝对时刻
    token_expires_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 批准后同事务生成的快照（驳回时保持为空；令牌经 snapshot_id 关联，不在此冗余）
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
