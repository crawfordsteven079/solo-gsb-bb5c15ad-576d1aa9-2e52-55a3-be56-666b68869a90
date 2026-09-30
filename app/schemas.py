from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class Interval(BaseModel):
    """左闭右开 [start, end)，按 Unicode 码点计。"""

    start: int = Field(ge=0)
    end: int = Field(ge=1)


class RecordCreateRequest(BaseModel):
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class RecordReviseRequest(BaseModel):
    base_version: int = Field(ge=1)
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class PublishRequest(BaseModel):
    version: int = Field(ge=1)
    # 两种到期方式二选一，均不填则令牌永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    # 可选的成功读取次数上限：正整数；不填则不限次
    max_reads: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _only_one_expiry(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class ControlledPublishRequest(PublishRequest):
    """受控发布申请：复用既有发布的版本、令牌到期与读取次数字段。"""


class ReviewRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class RecordResponse(BaseModel):
    record_id: UUID
    version: int


class ControlledFlagResponse(BaseModel):
    record_id: UUID
    controlled: bool


class SnapshotInfo(BaseModel):
    snapshot_id: UUID
    version: int
    status: str
    published_at: datetime


class RecordStatusResponse(BaseModel):
    record_id: UUID
    current_version: int
    controlled: bool
    snapshots: list[SnapshotInfo]


class PublishResponse(BaseModel):
    snapshot_id: UUID
    record_id: UUID
    version: int
    status: str
    token: str
    expires_at: datetime | None
    # 本令牌的成功读取次数上限；null 表示不限次
    max_reads: int | None


class TokenUsageResponse(BaseModel):
    """维护侧令牌用量查询：只含计数元数据，不含快照原文。"""

    token: str
    snapshot_id: UUID
    max_reads: int | None
    read_count: int
    # 剩余次数；max_reads 为 null（不限次）时同样为 null
    remaining_reads: int | None
    expires_at: datetime | None
    revoked: bool
    revoked_at: datetime | None
    created_at: datetime


class RevokeResponse(BaseModel):
    token: str
    revoked: bool
    already_revoked: bool


class PublicSnapshotResponse(BaseModel):
    record_id: UUID
    version: int
    status: str
    redacted_text: str


class PublishRequestInfo(BaseModel):
    """申请详情：只含元数据，绝不返回冻结原文。"""

    request_id: UUID
    record_id: UUID
    version: int
    status: str
    requested_by: str
    reviewed_by: str | None
    reason: str | None
    # 提交时选择的令牌到期策略；批准后 token_expires_at 为实际绝对到期时刻
    token_expires_in_seconds: int | None
    token_expires_at: datetime | None
    # 申请时冻结的读取次数上限（null 表示不限次）
    token_max_reads: int | None
    snapshot_id: UUID | None
    created_at: datetime
    decided_at: datetime | None


class PublishRequestListResponse(BaseModel):
    record_id: UUID
    requests: list[PublishRequestInfo]


class ControlledSubmitResponse(BaseModel):
    request_id: UUID
    record_id: UUID
    version: int
    status: str
    requested_by: str
    created_at: datetime


class ApproveResponse(BaseModel):
    request_id: UUID
    record_id: UUID
    version: int
    status: str
    reviewed_by: str
    snapshot_id: UUID
    token: str
    expires_at: datetime | None
    # 签发令牌的成功读取次数上限；null 表示不限次
    max_reads: int | None
