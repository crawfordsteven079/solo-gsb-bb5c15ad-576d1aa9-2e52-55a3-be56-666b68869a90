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

    # ---- 读取次数上限（直接发布） ----

    def _direct_publish(self, version=1, body_extra=None):
        body = {"version": version}
        if body_extra:
            body.update(body_extra)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json=body,
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_direct_publish_response_reports_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        self.assertEqual(pub["max_reads"], 2)
        # 不设上限时该字段为 None（再次发布复用同一快照，签出新令牌）
        pub_unlimited = self._direct_publish()
        self.assertIsNone(pub_unlimited["max_reads"])
        self.assertEqual(pub_unlimited["snapshot_id"], pub["snapshot_id"])

    def test_max_reads_must_be_positive_integer(self):
        for bad in (0, -1):
            r = self.client.post(
                f"/records/{self.record_id}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": bad},
            )
            self.assertEqual(r.status_code, 422, r.text)
        # 非整数同样 422
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 1.5},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_unlimited_token_can_be_read_repeatedly(self):
        pub = self._direct_publish()
        self.assertIsNone(pub["max_reads"])
        sid, tok = pub["snapshot_id"], pub["token"]
        for _ in range(7):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        status_body = r.json()
        self.assertIsNone(status_body["max_reads"])
        self.assertIsNone(status_body["remaining_reads"])
        self.assertEqual(status_body["reads_used"], 7)
        self.assertTrue(status_body["active"])

    def test_capped_token_allows_exactly_max_reads_then_403(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 前两次成功
        for expected_used in (1, 2):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("redacted_text", r.json())
            q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
            self.assertEqual(q["reads_used"], expected_used)
            self.assertEqual(q["remaining_reads"], 2 - expected_used)
        # 第三次起沿用既有模糊拒绝
        for _ in range(3):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        # 用尽后计数不再增长，维护侧显示已耗尽
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertEqual(q["remaining_reads"], 0)
        self.assertFalse(q["active"])
        # 维护侧响应不含原文
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertNotIn(TEXT_V1, r.text)

    def test_invalid_revoked_and_mismatched_requests_do_not_consume(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 第一次成功
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        # 无效令牌不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token=wrong-token")
        self.assertEqual(r.status_code, 403)
        # 令牌与快照不匹配：给同一快照再签发一个令牌，交叉使用
        other = self._read_token_for_same_snapshot_via_republish(
            sid, body_extra={"max_reads": 2}
        )
        self.assertNotEqual(other, tok)
        r = self.client.get(f"/public/snapshots/{sid}?token={other}")
        self.assertEqual(r.status_code, 200, r.text)
        # 用 other 访问一个不存在的快照：403，不消耗 other
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token={other}"
        )
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{other}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        # 撤销后读取 403，不消耗剩余次数
        self.assertEqual(
            self.client.post(f"/tokens/{tok}/revoke", headers=SERVICE_HEADERS).status_code,
            200,
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertIsNotNone(q["revoked_at"])

    def _read_token_for_same_snapshot_via_republish(self, existing_snapshot_id, body_extra=None):
        """同一快照可由再次直接发布签发出另一支令牌（ON CONFLICT 复用快照）。"""
        pub = self._direct_publish(body_extra=body_extra)
        self.assertEqual(pub["snapshot_id"], existing_snapshot_id)
        return pub["token"]

    def test_distinct_tokens_on_same_snapshot_count_independently(self):
        pub1 = self._direct_publish(body_extra={"max_reads": 1})
        sid = pub1["snapshot_id"]
        tok1 = pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 第二次直接发布未携带 max_reads -> 新令牌不限次
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertIsNone(q2["max_reads"])
        # tok1 用尽
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 403
        )
        # tok2 不受影响，继续可读
        for _ in range(4):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
            )
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q2["reads_used"], 4)

    def test_expiry_or_revocation_takes_precedence_over_remaining_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 5, "expires_in_seconds": 1})
        sid, tok = pub["snapshot_id"], pub["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        import time as _time

        _time.sleep(1.2)
        # 仍有剩余次数，但已到期 -> 403，不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertEqual(q["remaining_reads"], 4)

    def test_concurrent_reads_never_exceed_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 10})
        sid, tok = pub["snapshot_id"], pub["token"]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(20)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 10, successes + failures)
        self.assertEqual(len(failures), 10)
        self.assertTrue(all(code == 403 for code in failures))
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 10)
        self.assertEqual(q["remaining_reads"], 0)

    def test_token_status_unknown_token_404_and_requires_service_key(self):
        r = self.client.get("/tokens/not-a-real-token", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/tokens/not-a-real-token")
        self.assertEqual(r.status_code, 401)

    # ---- 读取次数上限（受控发布：申请冻结、批准生效） ----

    def test_controlled_max_reads_frozen_at_submission_and_effective_after_approval(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 申请详情可见冻结的上限（不泄露原文）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 2)
        self.assertNotIn(TEXT_V1, r.text)
        # 批准签发结果携带上限
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_reads"], 2)
        sid, tok = r.json()["snapshot_id"], r.json()["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_controlled_reject_creates_no_token_or_counters(self):
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 3})
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "不予发布"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 3)
        with SessionLocal() as session:
            # 驳回不产生快照/令牌（限定本记录，不受其他用例数据影响）
            token_count = (
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
            self.assertEqual(token_count, 0)
            snapshot_count = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .count()
            )
            self.assertEqual(snapshot_count, 0)
        # 驳回后不能批准
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )

    def test_controlled_max_reads_must_be_positive(self):
        self._mark_controlled()
        r = self._submit(version=1, expires={"max_reads": 0}, expected=422)
        self.assertNotIn(TEXT_V1, r.text)

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

    def test_existing_db_gets_read_limit_columns(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        # 先发布一支令牌，模拟既有部署的 read_tokens / publish_requests 数据
        pub = self._direct_publish()
        token = pub["token"]
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN max_reads"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN reads_used"))
            conn.execute(text("ALTER TABLE publish_requests DROP COLUMN token_max_reads"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        token_columns = {c["name"] for c in inspector.get_columns("read_tokens")}
        request_columns = {c["name"] for c in inspector.get_columns("publish_requests")}
        self.assertIn("max_reads", token_columns)
        self.assertIn("reads_used", token_columns)
        self.assertIn("token_max_reads", request_columns)

        from app.db import SessionLocal
        from app.models import PublishRequest as PRModel
        from app.models import ReadToken as RTModel

        with SessionLocal() as session:
            # 既有令牌迁移后继续不限次，已用次数为 0
            existing = session.get(RTModel, token)
            self.assertIsNone(existing.max_reads)
            self.assertEqual(existing.reads_used, 0)
            pr = session.get(PRModel, request_id)
            self.assertIsNone(pr.token_max_reads)

        # 服务升级后既有令牌仍可继续不限次读取
        r = self.client.get(
            f"/public/snapshots/{pub['snapshot_id']}?token={token}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(
            f"/public/snapshots/{pub['snapshot_id']}?token={token}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        q = self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertIsNone(q["remaining_reads"])


if __name__ == "__main__":
    unittest.main()
