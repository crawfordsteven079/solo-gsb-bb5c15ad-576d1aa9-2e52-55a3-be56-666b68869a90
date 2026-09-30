"""受控发布端到端集成测试：需要真实 PostgreSQL。

默认尝试用 pgserver（pip 包，内置 Postgres 16 二进制）自动拉起临时库；
也可通过环境变量 TEST_DATABASE_URL 指定已有数据库。

运行：
    python3 -m unittest tests.test_integration_api -v
"""

import json
import os
import threading
import unittest
from datetime import datetime, timezone

# 环境变量必须在导入 app.config 之前设置
os.environ.setdefault("SERVICE_KEY", "dev-service-key-change-me")
os.environ.setdefault(
    "REVIEW_CREDENTIALS",
    json.dumps(
        [
            {"username": "alice", "password": "alice-pw", "roles": ["operator"]},
            {"username": "bob", "password": "bob-pw", "roles": ["reviewer"]},
            {"username": "carol", "password": "carol-pw", "roles": ["operator", "reviewer"]},
        ]
    ),
)

_PG_SERVER = None
_SKIP_REASON = ""
if "TEST_DATABASE_URL" not in os.environ:
    try:
        import pgserver

        _PG_SERVER = pgserver.get_server("/tmp/pgdata-integration-test")
        # unix socket 形式：postgresql+psycopg2://postgres@/postgres?host=<socketdir>
        os.environ["DATABASE_URL"] = (
            "postgresql+psycopg2://postgres@/postgres?host=/tmp/pgdata-integration-test"
        )
    except Exception as exc:  # pragma: no cover - 环境不可用时跳过
        _PG_SERVER = None
        _SKIP_REASON = f"pgserver unavailable: {exc}"
else:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app.models import PublishRequest as PublishRequestModel  # noqa: E402

SKIP_INTEGRATION = _PG_SERVER is None and "TEST_DATABASE_URL" not in os.environ

SERVICE_HEADERS = {"X-Service-Key": "dev-service-key-change-me"}
BASIC_ALICE = ("alice", "alice-pw")
BASIC_BOB = ("bob", "bob-pw")
BASIC_CAROL = ("carol", "carol-pw")

TEXT_V1 = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"
TEXT_V2 = "完全不同的新原文，敏感词ABC。"
INTERVALS_V1 = [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ControlledPublishIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 建表（幂等）并通过 TestClient 上下文触发 lifespan 之外的显式初始化
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 每个用例使用全新记录，避免相互干扰
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": TEXT_V1, "intervals": INTERVALS_V1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.record_id = r.json()["record_id"]

    # ---- 工具方法 ----

    def _mark_controlled(self):
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["controlled"])

    def _submit(self, auth=BASIC_ALICE, version=1, expires=None, expected=201):
        body = {"version": version}
        if expires is not None:
            body.update(expires)
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", auth=auth, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 基础鉴权 ----

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_service_key_endpoints_still_require_key(self):
        r = self.client.post("/records", json={"text": "x", "intervals": []})
        self.assertEqual(r.status_code, 401)
        r = self.client.put(f"/records/{self.record_id}/controlled")
        self.assertEqual(r.status_code, 401)

    def test_controlled_endpoints_require_basic_auth(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", json={"version": 1}
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        # 错误口令
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("alice", "wrong"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)
        # 未知用户
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("nobody", "x"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)

    # ---- 受控标记与直接发布互斥 ----

    def test_mark_controlled_is_idempotent(self):
        self._mark_controlled()
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["controlled"])

    def test_mark_controlled_unknown_record_404(self):
        import uuid

        r = self.client.put(f"/records/{uuid.uuid4()}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_direct_publish_rejected_for_controlled_record(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(TEXT_V1, r.text)
        # 未留下任何快照或令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_submit_rejected_for_non_controlled_record(self):
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 角色校验 ----

    def test_operator_role_required_to_submit(self):
        self._mark_controlled()
        # bob 只有 reviewer 角色，不能提交
        r = self._submit(auth=BASIC_BOB, expected=403)
        self.assertNotIn(TEXT_V1, r.text)

    def test_reviewer_role_required_to_approve(self):
        self._mark_controlled()
        self._submit()
        # alice 只有 operator 角色，不能复核
        r = self.client.post(
            f"/publish-requests/{self._latest_request_id()}/approve", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 403)

    def _latest_request_id(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    # ---- 未知资源 ----

    def test_unknown_record_and_version(self):
        import uuid

        self._mark_controlled()
        r = self.client.post(
            f"/records/{uuid.uuid4()}/publish-requests", auth=BASIC_ALICE, json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        r = self._submit(version=99, expected=404)
        self.assertNotIn(TEXT_V1, r.text)

    def test_unknown_request_approve_reject_get(self):
        import uuid

        rid = str(uuid.uuid4())
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/approve", auth=BASIC_BOB).status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/reject", auth=BASIC_BOB, json={}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/publish-requests/{rid}", auth=BASIC_BOB).status_code, 404
        )

    # ---- 申请冻结版本内容 ----

    def test_request_freezes_version_content_at_submission(self):
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()
        # 提交后修订到版本 2
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 批准沿用冻结的版本 1 内容
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["status"], "approved")
        token = body["token"]
        snapshot_id = body["snapshot_id"]
        # 公开读取得到版本 1 的脱敏正文，而非版本 2 原文
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        self.assertNotIn(TEXT_V2, r.text)
        # 申请详情不泄露冻结原文
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertNotIn(TEXT_V1, r.text)
        self.assertNotIn("身份证", r.text)

    def test_approved_token_follows_existing_token_rules(self):
        self._mark_controlled()
        self._submit(version=1, expires={"expires_in_seconds": 3600})
        request_id = self._latest_request_id()
        # 提交后到批准前，申请保存的是相对时长策略（尚无绝对到期时刻）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["token_expires_in_seconds"], 3600)
        self.assertIsNone(r.json()["token_expires_at"])
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        # 到期时刻在批准签发时换算，约为批准时刻 + 3600s
        expires_at = r.json()["expires_at"]
        self.assertIsNotNone(expires_at)
        self.assertGreater(
            datetime.fromisoformat(expires_at), datetime.now(timezone.utc)
        )
        # 批准后申请记录实际绝对到期时刻
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertIsNotNone(r.json()["token_expires_at"])
        # 令牌可通过既有撤销接口撤销，撤销后公开读取 403
        r = self.client.post(f"/tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_submit_with_past_expires_at_is_422(self):
        self._mark_controlled()
        r = self._submit(
            expires={"expires_at": "2000-01-01T00:00:00Z"}, expected=422
        )
        self.assertNotIn(TEXT_V1, r.text)

    def test_approval_failure_rolls_back_snapshot_and_token(self):
        """批准事务中途失败时，不得留下快照或可读令牌。"""
        from unittest import mock

        from sqlalchemy import select

        from app import main as main_module
        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()

        # 让令牌在 flush 时超长（read_tokens.token 为 varchar(64)）：
        # 此时快照 INSERT 已执行、令牌已加入事务，但尚未提交。
        with mock.patch.object(
            main_module.secrets, "token_urlsafe", return_value="x" * 65
        ):
            with self.assertRaises(Exception):
                self.client.post(
                    f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
                )

        with SessionLocal() as session:
            # 申请仍为 pending（终态未写入），快照与令牌全部回滚
            pr = session.get(PublishRequestModel, request_id)
            self.assertEqual(pr.status, "pending")
            self.assertIsNone(pr.snapshot_id)
            snapshots = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .all()
            )
            self.assertEqual(snapshots, [])
            orphan_tokens = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(orphan_tokens, 0)
        # 申请仍可被正常批准（证明前一次没有遗留半完成状态）
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    def test_explicit_expires_at_is_kept_at_approval(self):
        from datetime import timedelta

        self._mark_controlled()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._submit(expires={"expires_at": future})
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["expires_at"])

    # ---- 申请人不得复核 ----

    def test_requester_cannot_review_own_request(self):
        # carol 同时具备 operator/reviewer，仍不得复核自己的申请
        self._mark_controlled()
        r = self._submit(auth=BASIC_CAROL)
        request_id = r.json()["request_id"]
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_CAROL)
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
        )
        self.assertEqual(r.status_code, 403)
        # 申请仍处于 pending，他人可正常批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    # ---- 终态语义 ----

    def test_rejected_request_cannot_be_approved(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "材料不全"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 驳回后不能批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409)
        # 再次驳回也是 409
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
        )
        self.assertEqual(r.status_code, 409)
        # 驳回未生成快照/令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_approved_request_cannot_be_decided_again(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
            ).status_code,
            409,
        )

    def test_concurrent_reviews_produce_single_terminal_state(self):
        """两个线程同时复核同一申请：恰好一个终态成功，另一个 409。"""
        self._mark_controlled()
        for _ in range(5):
            self._submit()
            request_id = self._latest_request_id()
            results: list[int] = []
            barrier = threading.Barrier(2)

            def approve():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB).status_code
                )

            def reject():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(
                        f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
                    ).status_code
                )

            t1 = threading.Thread(target=approve)
            t2 = threading.Thread(target=reject)
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(results), [200, 409], results)
            # 库里申请唯一终态，且至多一份快照
            from app.db import SessionLocal
            from app.models import Snapshot

            with SessionLocal() as session:
                pr = session.get(PublishRequestModel, request_id)
                self.assertIn(pr.status, ("approved", "rejected"))
                self.assertIsNotNone(pr.reviewed_by)
            r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
            self.assertLessEqual(len(r.json()["snapshots"]), 1)

    # ---- 既有接口兼容性（非受控记录） ----

    def test_legacy_flow_unchanged_on_non_controlled_record(self):
        # 直接发布非受控记录仍可用
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        # 状态接口新增字段不破坏既有结构
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        body = r.json()
        self.assertFalse(body["controlled"])
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(len(body["snapshots"]), 1)

    def test_legacy_revision_409_and_422_still_work(self):
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 99, "text": "x", "intervals": []},
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": "短", "intervals": [{"start": 0, "end": 99}]},
        )
        self.assertEqual(r.status_code, 422)

    def test_public_read_unified_403_does_not_leak(self):
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token=definitely-not-a-token"
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    # ---- 数据库升级路径 ----

    def test_existing_db_gets_controlled_column(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE records DROP COLUMN controlled"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("records")}
        self.assertIn("controlled", columns)
        # 旧行默认 false，数据未损坏
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["controlled"])

    # ---- 读取次数上限（本轮新增） ----

    def _publish(self, version=1, max_reads=..., expires=None, expected=201):
        body = {"version": version}
        if max_reads is not ...:
            body["max_reads"] = max_reads
        if expires is not None:
            body.update(expires)
        r = self.client.post(
            f"/records/{self.record_id}/publish", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _read(self, snapshot_id, token, expected=200):
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _usage(self, token, expected=200):
        r = self.client.get(f"/tokens/{token}/usage", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def test_direct_publish_response_shows_max_reads(self):
        r = self._publish(max_reads=3)
        self.assertEqual(r.json()["max_reads"], 3)
        # 不设置上限时签发结果为 null（不限次）
        r = self._publish(max_reads=None)
        self.assertIsNone(r.json()["max_reads"])

    def test_non_positive_max_reads_is_422(self):
        self._publish(max_reads=0, expected=422)
        self._publish(max_reads=-1, expected=422)
        # 被拒绝的请求不落库任何快照/令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_limited_token_exhausts_then_blurry_403(self):
        pub = self._publish(max_reads=2).json()
        sid, token = pub["snapshot_id"], pub["token"]
        # 前两次成功并逐次消耗
        self._read(sid, token)
        self._read(sid, token)
        # 达到上限后沿用既有模糊拒绝
        r = self._read(sid, token, expected=403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")
        # 超额后的拒绝不再增加计数
        self._read(sid, token, expected=403)
        usage = self._usage(token).json()
        self.assertEqual(usage["max_reads"], 2)
        self.assertEqual(usage["read_count"], 2)
        self.assertEqual(usage["remaining_reads"], 0)
        self.assertFalse(usage["revoked"])

    def test_unlimited_token_keeps_unlimited_reads(self):
        pub = self._publish(max_reads=None).json()
        sid, token = pub["snapshot_id"], pub["token"]
        for _ in range(6):
            self._read(sid, token)
        usage = self._usage(token).json()
        self.assertIsNone(usage["max_reads"])
        self.assertIsNone(usage["remaining_reads"])
        self.assertEqual(usage["read_count"], 6)

    def test_separate_tokens_for_same_snapshot_count_independently(self):
        # 同一快照（同 record+version 的重复发布只产生一份快照）的两个令牌各自计数
        first = self._publish(version=1, max_reads=1).json()
        sid = first["snapshot_id"]
        second = self._publish(version=1, max_reads=2).json()
        self.assertEqual(second["snapshot_id"], sid)
        t1, t2 = first["token"], second["token"]
        self._read(sid, t1)
        # 令牌 1 用尽，不影响令牌 2
        self._read(sid, t1, expected=403)
        self._read(sid, t2)
        self._read(sid, t2)
        self._read(sid, t2, expected=403)
        self.assertEqual(self._usage(t1).json()["read_count"], 1)
        self.assertEqual(self._usage(t2).json()["read_count"], 2)

    def test_failed_reads_do_not_consume(self):
        import uuid

        pub = self._publish(max_reads=3).json()
        sid, token = pub["snapshot_id"], pub["token"]
        # 令牌与快照不匹配
        other = self._publish(version=1, max_reads=3).json()
        # 其他记录的快照与本令牌不匹配（新建一条记录再发布）
        r = self.client.post(
            "/records", headers=SERVICE_HEADERS,
            json={"text": "另一条记录正文XYZ", "intervals": [{"start": 3, "end": 6}]},
        )
        other_record = r.json()["record_id"]
        r = self.client.post(
            f"/records/{other_record}/publish",
            headers=SERVICE_HEADERS, json={"version": 1},
        )
        other_sid = r.json()["snapshot_id"]
        self._read(other_sid, token, expected=403)
        # 快照不存在 / 令牌无效
        self._read(sid, "not-a-real-token", expected=403)
        self._read(str(uuid.uuid4()), token, expected=403)
        # 全部失败后额度原样保留：仍可成功读取 3 次
        self._read(sid, token)
        self._read(sid, token)
        self._read(sid, token)
        self._read(sid, token, expected=403)
        self.assertEqual(self._usage(token).json()["read_count"], 3)
        # 未使用：other 令牌仍满额（其快照被一次失败请求引用但未计数）
        self.assertEqual(self._usage(other["token"]).json()["read_count"], 0)

    def test_revocation_takes_precedence_over_remaining_reads(self):
        pub = self._publish(max_reads=10).json()
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        # 尚余 9 次，但撤销优先生效
        r = self.client.post(f"/tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        self._read(sid, token, expected=403)
        usage = self._usage(token).json()
        self.assertTrue(usage["revoked"])
        self.assertEqual(usage["read_count"], 1)
        self.assertEqual(usage["remaining_reads"], 9)

    def test_expiry_takes_precedence_over_remaining_reads(self):
        pub = self._publish(max_reads=10, expires={"expires_in_seconds": 1}).json()
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        # 到期后即使额度未用尽也一律拒绝，且不消耗
        import time as _time

        _time.sleep(1.2)
        self._read(sid, token, expected=403)
        usage = self._usage(token).json()
        self.assertEqual(usage["read_count"], 1)

    def test_concurrent_reads_never_exceed_limit(self):
        limit = 20
        pub = self._publish(max_reads=limit).json()
        sid, token = pub["snapshot_id"], pub["token"]
        attempts = 50
        results: list[int] = []
        barrier = threading.Barrier(attempts)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            r = client.get(f"/public/snapshots/{sid}?token={token}")
            results.append(r.status_code)

        threads = [threading.Thread(target=read_once) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), limit, results)
        self.assertEqual(results.count(403), attempts - limit)
        usage = self._usage(token).json()
        self.assertEqual(usage["read_count"], limit)
        self.assertEqual(usage["remaining_reads"], 0)

    def test_token_usage_requires_service_key_and_hides_text(self):
        import uuid

        pub = self._publish(max_reads=1).json()
        sid, token = pub["snapshot_id"], pub["token"]
        # 无密钥 401
        self.assertEqual(self.client.get(f"/tokens/{token}/usage").status_code, 401)
        # 未知令牌 404
        self.assertEqual(self._usage("nope", expected=404).status_code, 404)
        # 正常用量响应只含元数据，不含快照/原文相关字段
        usage = self._usage(token).json()
        self.assertEqual(usage["snapshot_id"], sid)
        self.assertNotIn("redacted_text", usage)
        self.assertNotIn("record_id", usage)
        self.assertEqual(usage["read_count"], 0)
        self.assertEqual(usage["remaining_reads"], 1)

    def test_controlled_request_freezes_max_reads_and_approval_effective(self):
        self._mark_controlled()
        # 申请时冻结上限；申请信息可见冻结值，尚未签发令牌
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=BASIC_ALICE, json={"version": 1, "max_reads": 2},
        )
        self.assertEqual(r.status_code, 201, r.text)
        request_id = r.json()["request_id"]
        info = self.client.get(
            f"/publish-requests/{request_id}", auth=BASIC_ALICE
        ).json()
        self.assertEqual(info["token_max_reads"], 2)
        # 批准签发后上限生效并回显
        r = self.client.post(
            f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_reads"], 2)
        sid, token = r.json()["snapshot_id"], r.json()["token"]
        self._read(sid, token)
        self._read(sid, token)
        self._read(sid, token, expected=403)
        self.assertEqual(self._usage(token).json()["read_count"], 2)

    def test_controlled_rejection_creates_no_token_or_count(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=BASIC_ALICE, json={"version": 1, "max_reads": 5},
        )
        request_id = r.json()["request_id"]
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB, json={"reason": "不予公开"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 5)
        # 驳回不生成快照或令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])
        from sqlalchemy import select as _select

        from app.db import SessionLocal
        from app.models import ReadToken as RT
        from app.models import Snapshot as Snap

        with SessionLocal() as session:
            tokens_for_record = (
                session.query(RT)
                .filter(
                    RT.snapshot_id.in_(
                        _select(Snap.id).where(Snap.record_id == self.record_id)
                    )
                )
                .count()
            )
            self.assertEqual(tokens_for_record, 0)

    def test_controlled_non_positive_max_reads_is_422(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=BASIC_ALICE, json={"version": 1, "max_reads": 0},
        )
        self.assertEqual(r.status_code, 422)

    def test_existing_db_gets_read_limit_columns(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE read_tokens DROP CONSTRAINT IF EXISTS ck_read_tokens_count_within_limit"))
            conn.execute(text("ALTER TABLE read_tokens DROP CONSTRAINT IF EXISTS ck_read_tokens_read_count"))
            conn.execute(text("ALTER TABLE read_tokens DROP CONSTRAINT IF EXISTS ck_read_tokens_max_reads"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN read_count"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN max_reads"))
            conn.execute(text("ALTER TABLE publish_requests DROP COLUMN token_max_reads"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        token_columns = {c["name"] for c in inspector.get_columns("read_tokens")}
        self.assertIn("max_reads", token_columns)
        self.assertIn("read_count", token_columns)
        request_columns = {c["name"] for c in inspector.get_columns("publish_requests")}
        self.assertIn("token_max_reads", request_columns)
        # 既有令牌迁移后不限次、计数为 0，仍可正常读取
        pub = self._publish(max_reads=None).json()
        sid, token = pub["snapshot_id"], pub["token"]
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE read_tokens DROP CONSTRAINT IF EXISTS ck_read_tokens_count_within_limit"))
            conn.execute(text("ALTER TABLE read_tokens DROP CONSTRAINT IF EXISTS ck_read_tokens_read_count"))
            conn.execute(text("ALTER TABLE read_tokens DROP CONSTRAINT IF EXISTS ck_read_tokens_max_reads"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN read_count"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN max_reads"))
        app_main._init_db_with_retry()
        self._read(sid, token)
        usage = self._usage(token).json()
        self.assertIsNone(usage["max_reads"])
        self.assertEqual(usage["read_count"], 1)
        self.assertIsNone(usage["remaining_reads"])


if __name__ == "__main__":
    unittest.main()
