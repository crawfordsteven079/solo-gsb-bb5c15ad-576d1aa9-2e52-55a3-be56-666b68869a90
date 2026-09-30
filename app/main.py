import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, inspect, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from .models import (
    REQUEST_APPROVED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    PublishRequest as PublishRequestModel,
    ReadToken,
    Record,
    RecordVersion,
    Snapshot,
)
from .redaction import IntervalOutOfBoundsError, redact_text, validate_intervals
from .schemas import (
    ApproveResponse,
    ControlledFlagResponse,
    ControlledPublishRequest,
    ControlledSubmitResponse,
    Interval,
    PublicSnapshotResponse,
    PublishRequest as PublishPayload,
    PublishRequestInfo,
    PublishRequestListResponse,
    PublishResponse,
    RecordCreateRequest,
    RecordResponse,
    RecordReviseRequest,
    RecordStatusResponse,
    ReviewRejectRequest,
    RevokeResponse,
    SnapshotInfo,
    TokenStatusResponse,
)
from .security import Operator, require_operator, require_role, require_service_key

SNAPSHOT_STATUS = "published"


def _init_db_with_retry() -> None:
    """启动时建表（幂等），并等待数据库就绪。"""
    last_exc: Exception | None = None
    for _ in range(settings.db_connect_retries):
        try:
            Base.metadata.create_all(engine)
            _apply_lightweight_migrations()
            return
        except OperationalError as exc:
            last_exc = exc
            time.sleep(settings.db_connect_retry_delay_seconds)
    raise RuntimeError("database is not reachable") from last_exc


def _apply_lightweight_migrations() -> None:
    """对既有库做幂等的加列/补列升级（仅用于本轮新增字段，安全可重复执行）。"""
    inspector = inspect(engine)
    table_columns = {column["name"] for column in inspector.get_columns("records")}
    statements: list[str] = []
    if "controlled" not in table_columns:
        statements.append(
            "ALTER TABLE records ADD COLUMN controlled BOOLEAN NOT NULL DEFAULT false"
        )
    if "read_tokens" in inspector.get_table_names():
        token_columns = {
            column["name"] for column in inspector.get_columns("read_tokens")
        }
        if "max_reads" not in token_columns:
            # 既有令牌不加上限，继续不限次
            statements.append("ALTER TABLE read_tokens ADD COLUMN max_reads INTEGER")
        if "reads_used" not in token_columns:
            statements.append(
                "ALTER TABLE read_tokens ADD COLUMN reads_used INTEGER NOT NULL DEFAULT 0"
            )
    if "publish_requests" in inspector.get_table_names():
        request_columns = {
            column["name"] for column in inspector.get_columns("publish_requests")
        }
        if "token_expires_in_seconds" not in request_columns:
            statements.append(
                "ALTER TABLE publish_requests "
                "ADD COLUMN token_expires_in_seconds INTEGER"
            )
        if "token_expires_at" not in request_columns:
            statements.append(
                "ALTER TABLE publish_requests ADD COLUMN token_expires_at TIMESTAMP"
            )
        if "token_max_reads" not in request_columns:
            statements.append(
                "ALTER TABLE publish_requests ADD COLUMN token_max_reads INTEGER"
            )
        # 早期版本曾冗余存放令牌；统一以 read_tokens 为准，删除冗余列
        if "published_token" in request_columns:
            statements.append("ALTER TABLE publish_requests DROP COLUMN published_token")
    if statements:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db_with_retry()
    yield


app = FastAPI(
    title="Redacted Records API",
    version="1.2.0",
    summary="机构公开脱敏记录服务（受控发布申请-复核 + 令牌读取次数上限）",
    lifespan=lifespan,
)


def _validated_intervals(text: str, intervals: list[Interval]) -> list[tuple[int, int]]:
    """越界区间直接 422，且发生在任何写库之前，保证整次拒绝。"""
    try:
        return validate_intervals(len(text), [(iv.start, iv.end) for iv in intervals])
    except IntervalOutOfBoundsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _future_expires_at(
    expires_in_seconds: int | None, expires_at: datetime | None
) -> datetime | None:
    """解析令牌到期时间；显式到期时刻必须晚于当前时间，否则 422。"""
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expires_at must be in the future",
            )
        return expires_at
    if expires_in_seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    return None


def _request_info(request: PublishRequestModel) -> PublishRequestInfo:
    """申请详情映射：只输出元数据，冻结原文绝不离开服务端。"""
    return PublishRequestInfo(
        request_id=request.id,
        record_id=request.record_id,
        version=request.version,
        status=request.status,
        requested_by=request.requested_by,
        reviewed_by=request.reviewed_by,
        reason=request.reason,
        token_expires_in_seconds=request.token_expires_in_seconds,
        token_expires_at=request.token_expires_at,
        token_max_reads=request.token_max_reads,
        snapshot_id=request.snapshot_id,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


def _validate_expiry_policy(
    expires_in_seconds: int | None, expires_at: datetime | None
) -> None:
    """校验提交时选择的令牌到期策略：绝对时刻必须晚于提交时刻。

    相对时长（expires_in_seconds）在批准签发时才换算，复核耗时不占用令牌寿命。
    """
    if expires_at is None:
        return
    normalized = expires_at if expires_at.tzinfo is not None else expires_at.replace(
        tzinfo=timezone.utc
    )
    if normalized <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expires_at must be in the future",
        )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post(
    "/records",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def create_record(payload: RecordCreateRequest, db: Session = Depends(get_db)):
    """创建记录（版本 1）：提交原文与敏感区间。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    record = Record(current_version=1)
    db.add(record)
    db.flush()
    db.add(
        RecordVersion(
            record_id=record.id,
            version=1,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record.id, version=1)


@app.put(
    "/records/{record_id}",
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def revise_record(record_id: UUID, payload: RecordReviseRequest, db: Session = Depends(get_db)):
    """修订记录：必须携带当前版本号作为基准，过期则 409 并返回当前版本。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    # 条件更新是原子的：并发修订时只有一个能基于同一基准版本成功，
    # 其余因 current_version 已变化而失败，不会产生分叉版本。
    new_version = db.execute(
        update(Record)
        .where(Record.id == record_id, Record.current_version == payload.base_version)
        .values(current_version=payload.base_version + 1)
        .returning(Record.current_version)
    ).scalar_one_or_none()
    if new_version is None:
        db.rollback()
        record = db.get(Record, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "base_version is stale",
                "current_version": record.current_version,
            },
        )
    db.add(
        RecordVersion(
            record_id=record_id,
            version=new_version,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record_id, version=new_version)


@app.put(
    "/records/{record_id}/controlled",
    response_model=ControlledFlagResponse,
    dependencies=[Depends(require_service_key)],
)
def mark_record_controlled(record_id: UUID, db: Session = Depends(get_db)):
    """将记录设为受控发布（幂等：已是受控状态时直接返回当前状态）。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if not record.controlled:
        record.controlled = True
        db.commit()
    return ControlledFlagResponse(record_id=record_id, controlled=True)


@app.post(
    "/records/{record_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=PublishResponse,
    dependencies=[Depends(require_service_key)],
)
def publish_record(record_id: UUID, payload: PublishPayload, db: Session = Depends(get_db)):
    """直接发布指定版本：合并区间生成不可变快照，并签发随机读取令牌。

    受控记录的直接发布请求一律 409 拒绝，必须走申请-复核流程。
    """
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    if record.controlled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is controlled; submit a publish request for review instead",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    expires_at = _future_expires_at(payload.expires_in_seconds, payload.expires_at)
    redacted = redact_text(
        version.original_text, [(iv["start"], iv["end"]) for iv in version.intervals]
    )
    # 快照按 (record_id, version) 唯一：并发发布时唯一约束保证只落库一份，
    # 且快照与令牌在同一事务提交，不会出现部分发布。
    db.execute(
        pg_insert(Snapshot)
        .values(record_id=record_id, version=payload.version, redacted_text=redacted)
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    snapshot = db.execute(
        select(Snapshot).where(
            Snapshot.record_id == record_id, Snapshot.version == payload.version
        )
    ).scalar_one()
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot.id,
        expires_at=expires_at,
        max_reads=payload.max_reads,
    )
    db.add(token)
    db.commit()
    return {
        "snapshot_id": snapshot.id,
        "record_id": record_id,
        "version": payload.version,
        "status": SNAPSHOT_STATUS,
        "token": token.token,
        "expires_at": expires_at,
        "max_reads": payload.max_reads,
    }


@app.post(
    "/records/{record_id}/publish-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=ControlledSubmitResponse,
)
def submit_publish_request(
    record_id: UUID,
    payload: ControlledPublishRequest,
    operator: Operator = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """提交受控发布申请：用申请人的个人凭据，冻结提交时刻的历史版本内容。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if not record.controlled:
        # 非受控记录应使用直接发布，明确拒绝以免误用流程
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is not controlled; use the direct publish endpoint",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    _validate_expiry_policy(payload.expires_in_seconds, payload.expires_at)
    expires_at = payload.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    # 待审内容在此冻结：后续修订只产生新版本，不改变本申请绑定的原文与区间
    request = PublishRequestModel(
        record_id=record_id,
        version=payload.version,
        frozen_text=version.original_text,
        frozen_intervals=[dict(iv) for iv in version.intervals],
        status=REQUEST_PENDING,
        requested_by=operator.username,
        token_expires_in_seconds=payload.expires_in_seconds,
        token_expires_at=expires_at,
        token_max_reads=payload.max_reads,
    )
    db.add(request)
    db.commit()
    return ControlledSubmitResponse(
        request_id=request.id,
        record_id=record_id,
        version=payload.version,
        status=request.status,
        requested_by=request.requested_by,
        created_at=request.created_at,
    )


@app.post(
    "/publish-requests/{request_id}/approve",
    response_model=ApproveResponse,
)
def approve_publish_request(
    request_id: UUID,
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """批准申请：沿用既有脱敏与令牌规则，同事务生成快照与新读取令牌。

    申请人不得复核；已终态（批准/驳回）的申请一律 409；
    行锁串行化并发复核，保证同一申请只有一个终态。
    """
    # SELECT ... FOR UPDATE：并发复核在此排队，后进入者会看到已终态并被拒绝
    request = db.execute(
        select(PublishRequestModel)
        .where(PublishRequestModel.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    if request.status != REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "publish request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own publish request",
        )

    # 快照与令牌在同一事务内生成：任一步失败整体回滚，不会留下可读令牌。
    # 相对到期时长在签发时刻换算为绝对时刻；显式到期时刻沿用申请时的选择。
    if request.token_expires_in_seconds is not None:
        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=request.token_expires_in_seconds
        )
    else:
        token_expires_at = request.token_expires_at
    redacted = redact_text(
        request.frozen_text,
        [(iv["start"], iv["end"]) for iv in request.frozen_intervals],
    )
    db.execute(
        pg_insert(Snapshot)
        .values(
            record_id=request.record_id,
            version=request.version,
            redacted_text=redacted,
        )
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    snapshot = db.execute(
        select(Snapshot).where(
            Snapshot.record_id == request.record_id,
            Snapshot.version == request.version,
        )
    ).scalar_one()
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot.id,
        expires_at=token_expires_at,
        max_reads=request.token_max_reads,
    )
    db.add(token)
    db.flush()  # 确保令牌已在事务内落库后再终结申请
    request.status = REQUEST_APPROVED
    request.reviewed_by = operator.username
    request.snapshot_id = snapshot.id
    request.token_expires_at = token_expires_at
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return ApproveResponse(
        request_id=request.id,
        record_id=request.record_id,
        version=request.version,
        status=request.status,
        reviewed_by=operator.username,
        snapshot_id=snapshot.id,
        token=token.token,
        expires_at=token_expires_at,
        max_reads=request.token_max_reads,
    )


@app.post(
    "/publish-requests/{request_id}/reject",
    response_model=PublishRequestInfo,
)
def reject_publish_request(
    request_id: UUID,
    payload: ReviewRejectRequest = Body(default_factory=ReviewRejectRequest),
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """驳回申请：不生成快照与令牌；驳回是终态，此后不能再批准。"""
    request = db.execute(
        select(PublishRequestModel)
        .where(PublishRequestModel.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    if request.status != REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "publish request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own publish request",
        )
    request.status = REQUEST_REJECTED
    request.reviewed_by = operator.username
    request.reason = payload.reason
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _request_info(request)


@app.get(
    "/publish-requests/{request_id}",
    response_model=PublishRequestInfo,
)
def get_publish_request(
    request_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """查看单个申请状态（任何已认证操作者可查；不返回冻结原文）。"""
    request = db.get(PublishRequestModel, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    return _request_info(request)


@app.get(
    "/records/{record_id}/publish-requests",
    response_model=PublishRequestListResponse,
)
def list_publish_requests(
    record_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """列出某条记录的全部受控发布申请（不返回冻结原文）。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    requests = (
        db.execute(
            select(PublishRequestModel)
            .where(PublishRequestModel.record_id == record_id)
            .order_by(PublishRequestModel.created_at)
        )
        .scalars()
        .all()
    )
    return PublishRequestListResponse(
        record_id=record_id, requests=[_request_info(request) for request in requests]
    )


@app.post(
    "/tokens/{token}/revoke",
    response_model=RevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_token(token: str, db: Session = Depends(get_db)):
    """撤销令牌：单条原子 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadToken)
        .where(ReadToken.token == token, ReadToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return RevokeResponse(token=token, revoked=True, already_revoked=False)
    if db.get(ReadToken, token) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    return RevokeResponse(token=token, revoked=True, already_revoked=True)


@app.get(
    "/tokens/{token}",
    response_model=TokenStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_token_status(token: str, db: Session = Depends(get_db)):
    """维护侧查询令牌的读取次数上限、已用与剩余次数（绝不返回快照原文）。"""
    read_token = db.get(ReadToken, token)
    if read_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    snapshot = db.get(Snapshot, read_token.snapshot_id)
    now = datetime.now(timezone.utc)
    expired = read_token.expires_at is not None and read_token.expires_at <= now
    exhausted = (
        read_token.max_reads is not None and read_token.reads_used >= read_token.max_reads
    )
    remaining = (
        None
        if read_token.max_reads is None
        else max(read_token.max_reads - read_token.reads_used, 0)
    )
    return TokenStatusResponse(
        token=read_token.token,
        snapshot_id=read_token.snapshot_id,
        record_id=snapshot.record_id,
        version=snapshot.version,
        created_at=read_token.created_at,
        expires_at=read_token.expires_at,
        revoked_at=read_token.revoked_at,
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,
        remaining_reads=remaining,
        active=read_token.revoked_at is None and not expired and not exhausted,
    )


@app.get(
    "/records/{record_id}",
    response_model=RecordStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_record(record_id: UUID, db: Session = Depends(get_db)):
    """维护侧查看记录当前版本、受控标记与已发布快照列表。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.record_id == record_id).order_by(Snapshot.version)
        )
        .scalars()
        .all()
    )
    return RecordStatusResponse(
        record_id=record.id,
        current_version=record.current_version,
        controlled=record.controlled,
        snapshots=[
            SnapshotInfo(
                snapshot_id=s.id,
                version=s.version,
                status=SNAPSHOT_STATUS,
                published_at=s.published_at,
            )
            for s in snapshots
        ],
    )


@app.get("/public/snapshots/{snapshot_id}", response_model=PublicSnapshotResponse)
def read_snapshot(
    snapshot_id: UUID,
    token: str = Query(..., description="发布时签发的读取令牌"),
    db: Session = Depends(get_db),
):
    """公开读取：凭有效令牌返回脱敏正文。

    有效性判定与次数消耗由同一条条件 UPDATE 原子完成：
    - 令牌不存在、与快照不匹配、已撤销、已到期或已达上限 -> rowcount=0，
      统一返回模糊的 403，且这些请求不消耗次数；
    - 撤销/到期优先于剩余次数判断，不能因尚有次数而放行；
    - 次数判断（reads_used < max_reads）与递增在同一行更新内完成，
      并发读取由数据库行锁串行化，成功次数绝不超过上限。
    """
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None:
        # 不区分“快照不存在 / 令牌无效 / 已撤销 / 已过期 / 次数用尽”
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    now = datetime.now(timezone.utc)
    # 只有“凭有效令牌成功读取”才计数；上限为 NULL 时继续不限次。
    result = db.execute(
        update(ReadToken)
        .where(
            ReadToken.token == token,
            ReadToken.snapshot_id == snapshot.id,
            ReadToken.revoked_at.is_(None),
            or_(ReadToken.expires_at.is_(None), ReadToken.expires_at > now),
            or_(
                ReadToken.max_reads.is_(None),
                ReadToken.reads_used < ReadToken.max_reads,
            ),
        )
        .values(reads_used=ReadToken.reads_used + 1)
    )
    if not result.rowcount:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    db.commit()
    return {
        "record_id": snapshot.record_id,
        "version": snapshot.version,
        "status": SNAPSHOT_STATUS,
        "redacted_text": snapshot.redacted_text,
    }
