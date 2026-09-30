# Redacted Records API

供机构公开发布脱敏记录的 HTTP 服务。维护人员凭服务密钥提交原文与敏感区间，发布时按 Unicode 码点区间合并遮蔽，生成**不可变快照**与**随机读取令牌**；公众仅凭有效令牌读取脱敏正文。

支持两种发布方式：

- **直接发布**（既有能力）：维护人员凭服务密钥直接发布指定版本；
- **受控发布**（本轮新增）：机构先把记录标记为受控，操作者用**各自的凭据与身份**提交指定历史版本的发布申请，由**另一名具复核资格者**批准或驳回（申请人不得复核）；批准时在同一事务生成不可变快照与新读取令牌。

签发快照读取令牌时可**可选**地设置正整数**读取次数上限**（`max_reads`）：直接发布在请求时确定，受控发布在提交申请时冻结、批准签发后生效。公开读取沿用既有脱敏正文响应，每次凭有效令牌**成功读取**消耗一次；无效、撤销、到期、令牌与快照不匹配或已达上限的请求不消耗次数。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档：`http://localhost:8000/docs`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与 API 两个容器，自动完成依赖安装与数据库初始化（启动时幂等建表，并重试等待数据库就绪）。数据持久化在命名卷 `pgdata` 中。

停止并清空数据：`docker compose down -v`

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SERVICE_KEY` | 维护人员服务密钥，**必填**，未配置服务拒绝启动 | 无（Compose 中为演示值 `dev-service-key-change-me`，生产务必修改） |
| `REVIEW_CREDENTIALS` | 受控发布操作者凭据，JSON 数组，见下 | `[]`（受控接口无人可用，不影响其他接口） |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://redact:redact@localhost:5432/redact` |
| `DB_CONNECT_RETRIES` | 启动时等待数据库的重试次数 | `30` |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | 重试间隔（秒） | `1.0` |

维护接口（创建、修订、直接发布、撤销、状态、标记受控）通过请求头 `X-Service-Key` 鉴权；缺失或不符一律 `401` 整次拒绝。

受控发布接口（提交、批准、驳回、查询申请）使用 **HTTP Basic 认证**（`Authorization: Basic base64(用户名:口令)`），按 `REVIEW_CREDENTIALS` 校验**操作者个人凭据**，并区分角色：

- `operator`：可提交发布申请；
- `reviewer`：可批准/驳回申请（申请人不得复核自己的申请）；
- 一人可同时具备两种角色（如机构的备用复核人）。

`REVIEW_CREDENTIALS` 为 JSON 数组，每项形如：

```json
[
  {"username": "alice", "password": "强口令A", "roles": ["operator"]},
  {"username": "bob",   "password": "强口令B", "roles": ["reviewer"]},
  {"username": "carol", "password": "强口令C", "roles": ["operator", "reviewer"]}
]
```

服务启动时校验该配置：用户名非空且不重复、口令非空、角色只能是 `operator`/`reviewer` 且至少一个，非法配置会让服务**快速失败**（报错信息不含口令）。至少需配置一名 `operator` 与一名**不同于**常用申请人的 `reviewer`，否则申请无法被独立复核。生产环境请通过机密管理系统注入，不要把真实口令提交到仓库。

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/records` | 服务密钥 | 创建记录（版本 1），提交原文与敏感区间 |
| `PUT` | `/records/{record_id}` | 服务密钥 | 修订：携带 `base_version`，过期返回 `409` 与当前版本 |
| `PUT` | `/records/{record_id}/controlled` | 服务密钥 | 将记录设为受控发布（幂等） |
| `POST` | `/records/{record_id}/publish` | 服务密钥 | 直接发布指定版本（可带 `max_reads` 上限）；**受控记录一律 `409` 拒绝** |
| `POST` | `/records/{record_id}/publish-requests` | operator | 提交受控发布申请（冻结指定历史版本内容与 `max_reads`） |
| `POST` | `/publish-requests/{request_id}/approve` | reviewer（非申请人） | 批准：同事务生成快照 + 新读取令牌（冻结的上限生效） |
| `POST` | `/publish-requests/{request_id}/reject` | reviewer（非申请人） | 驳回（可带 `reason`），驳回为终态，不生成令牌或计数 |
| `GET` | `/publish-requests/{request_id}` | 任一已认证操作者 | 查询单个申请状态（含冻结的 `token_max_reads`，不含原文） |
| `GET` | `/records/{record_id}/publish-requests` | 任一已认证操作者 | 列出某记录的全部申请（不含原文） |
| `GET` | `/tokens/{token}` | 服务密钥 | 查询令牌状态：上限 `max_reads`、已用 `reads_used`、剩余 `remaining_reads`（不含原文） |
| `POST` | `/tokens/{token}/revoke` | 服务密钥 | 撤销读取令牌（含受控发布签发的令牌） |
| `GET` | `/records/{record_id}` | 服务密钥 | 查看当前版本、受控标记与快照列表 |
| `GET` | `/public/snapshots/{snapshot_id}?token=...` | 无（凭令牌） | 公开读取脱敏正文 |
| `GET` | `/healthz` | 无 | 健康检查 |

### 区间语义

- 左闭右开 `[start, end)`，位置按 **Unicode 码点**计（与 Python 字符串下标一致，BMP 之外的字符算 1 个码点）。
- 越界、为空或倒置的区间 → `422`，**整次拒绝**，不落库任何内容。
- 发布时合并**相交或相邻**的区间，每段替换为 `[已遮蔽]`，其余原文保留。

### 读取次数上限（`max_reads`）

签发读取令牌时可**可选**地携带正整数 `max_reads`（不传或为 `null` 表示**不限次**，既有及新令牌默认如此）：

- **确定/冻结时点**：直接发布在发布请求中确定；受控发布在 `POST .../publish-requests` 提交申请时**冻结**（申请详情以 `token_max_reads` 返回），复核耗时与后续修订不改变它，**批准签发后**才落到新令牌上；申请被驳回不生成令牌或计数。
- **计数口径**：每次凭**有效**令牌成功读取脱敏正文消耗一次；`reads_used` 只增不减。同一快照的**不同令牌各自独立计数**（同一版本重复发布会复用快照、签出新令牌）。
- **达上限后**：读取沿用既有模糊拒绝 `403 snapshot unavailable`，公开响应结构不变，不额外暴露“已用尽”这一原因。
- **不消耗次数的请求**：令牌无效、已撤销、已到期，或令牌与快照不匹配（含快照不存在）。
- **撤销/到期优先**：即使尚有剩余次数，撤销或到期后读取一律 `403`，不能因尚有次数而放行。
- **并发安全**：次数判定与递增由公开读取接口内的**同一条条件 `UPDATE`** 原子完成（`reads_used < max_reads` 命中行锁串行化），并发读取的成功次数绝不超过上限；撤销是独立的原子 `UPDATE`，提交后优先生效。
- **维护侧查询**：`GET /tokens/{token}`（服务密钥）返回 `max_reads` / `reads_used` / `remaining_reads`（不限次时后两者中的上限与剩余为 `null`）及 `active` 等元数据；该接口**不返回快照原文或脱敏正文**，未知令牌 `404`，缺密钥 `401`。

### 调用示例

```bash
# 1. 创建记录（版本 1）
curl -sS -X POST http://localhost:8000/records \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
        "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
      }'
# => {"record_id":"<RID>","version":1}

# 2. 修订（base_version 过期时返回 409 与当前版本）
curl -sS -X PUT http://localhost:8000/records/<RID> \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"base_version": 1, "text": "……新原文……", "intervals": [{"start": 0, "end": 3}]}'
# => {"record_id":"<RID>","version":2}
# 过期时 => 409 {"detail":{"message":"base_version is stale","current_version":2}}

# 3. 发布版本 2（可另加 "expires_in_seconds": 3600、"expires_at": "2026-10-01T00:00:00Z"、"max_reads": 5）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"version": 2, "max_reads": 5}'
# => {"snapshot_id":"<SID>","version":2,"status":"published","token":"<TOKEN>","expires_at":null,"max_reads":5}

# 3b. 维护侧查询已用/剩余次数（不限次令牌 max_reads 与 remaining_reads 为 null）
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"token":"<TOKEN>","snapshot_id":"<SID>","record_id":"<RID>","version":2,
#     "max_reads":5,"reads_used":0,"remaining_reads":5,"active":true, ...}

# 4. 公开读取（无需密钥，只返回脱敏正文）；前 5 次 200，第 6 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"患者[已遮蔽]，……"}

# 5. 撤销令牌（撤销完成后，即使尚有剩余次数，新开始的读取一律 403）
curl -sS -X POST http://localhost:8000/tokens/<TOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"
```

完整端到端演示（含 401/409/422、快照不可变、`max_reads` 次数上限与独立计数、维护侧计数查询、撤销与到期）：

```bash
./examples/demo.sh        # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖默认配置
```

## 受控发布（申请-复核）

受控记录**不能**直接发布，必须走申请-复核流程。

### 语义与规则

1. **标记受控**：机构凭服务密钥 `PUT /records/{id}/controlled`（幂等）。受控记录的直接发布请求返回 `409`；非受控记录的受控申请同样返回 `409`（引导使用正确入口）。
2. **提交申请**：具备 `operator` 角色者用**本人凭据** `POST /records/{id}/publish-requests`，指定一个**历史版本**（`version`，可带 `expires_in_seconds`/`expires_at` 与可选正整数 `max_reads`，规则与直接发布一致）。申请在提交时**冻结该版本的原文与敏感区间，以及读取次数上限**，后续修订只产生新版本，不改变待审内容与上限。
3. **复核**：具备 `reviewer` 角色的**另一人**批准或驳回：
   - **申请人不得复核**自己的申请（即使其同时拥有 reviewer 角色）→ `403`；
   - 批准与驳回互斥：驳回后不能批准、批准后不能再驳回；对已终结申请再次操作 → `409`；
   - 并发复核由数据库行锁串行化，**同一申请只会产生一个终态**，落选方收到 `409`。
4. **批准事务**：沿用既有脱敏（合并相交/相邻区间→`[已遮蔽]`）与令牌（`secrets.token_urlsafe(32)`、可选到期）规则，在**同一事务**内生成不可变快照、写入新读取令牌（携带申请时冻结的 `max_reads`）并终结申请；任何一步失败整体回滚，**不会留下可读令牌或计数**。
5. **驳回**：只终结申请（可记录 `reason`），不生成快照、令牌或读取计数。
6. **拒绝不泄露原文**：无凭据/凭据错误 → `401`，无角色 → `403`，未知记录/版本/申请 → `404`，已终结/入口错误 → `409`；申请查询只返回元数据（含 `token_max_reads`），冻结原文绝不离开服务端。
7. 批准签发的令牌与直接发布令牌完全一致：遵守 `max_reads` 次数上限与到期规则，可用 `POST /tokens/{token}/revoke` 撤销，可用 `GET /tokens/{token}` 查询已用/剩余次数。

### 调用示例

```bash
# 0. 机构把记录设为受控（服务密钥；幂等）
curl -sS -X PUT http://localhost:8000/records/<RID>/controlled \
  -H "X-Service-Key: dev-service-key-change-me"
# => {"record_id":"<RID>","controlled":true}

# 0b. 受控记录的直接发布请求被明确拒绝（不生成快照）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"version": 1}'
# => 409 {"detail":"record is controlled; submit a publish request for review instead"}

# 1. 申请人 alice（operator）用本人凭据提交历史版本 1 的发布申请
#    max_reads 在申请时冻结，批准签发后生效（驳回则不生成令牌/计数）
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"version": 1, "max_reads": 3}'
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"pending",
#     "requested_by":"alice","created_at":"..."}

# 1b. 申请详情可见冻结的上限（只含元数据，不含原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
# => {...,"token_expires_in_seconds":null,"token_expires_at":null,"token_max_reads":3,...}

# 2. 申请人不能复核自己的申请
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own publish request"}

# 3. 另一人 bob（reviewer）批准：同事务生成不可变快照 + 新读取令牌
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u bob:bob-pw
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"approved",
#     "reviewed_by":"bob","snapshot_id":"<SID>","token":"<TOKEN>","expires_at":null,"max_reads":3}

# 4. 公众凭令牌读取冻结版本的脱敏正文：恰好 3 次 200，第 4 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":1,"status":"published","redacted_text":"患者[已遮蔽]，……"}
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"max_reads":3,"reads_used":1,"remaining_reads":2,"active":true,...}（不含原文）

# —— 驳回路径 ——
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" -d '{"version": 2}'
# => {"request_id":"<PRID2>",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/reject \
  -u bob:bob-pw -H "Content-Type: application/json" -d '{"reason":"材料不全"}'
# => {"request_id":"<PRID2>","status":"rejected","reviewed_by":"bob","reason":"材料不全",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/approve -u bob:bob-pw
# => 409 {"detail":{"message":"publish request already decided","status":"rejected"}}

# 查询（任一已认证操作者；只返回元数据，不返回原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
curl -sS http://localhost:8000/records/<RID>/publish-requests -u alice:alice-pw
```

完整受控流程演示（含 401/403/404/409、冻结内容与 `max_reads`、申请人回避、并发复核终态唯一、次数用尽与撤销）：

```bash
./examples/demo_controlled.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（受控接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少所需角色（operator/reviewer） | `403` |
| 申请人复核自己的申请 | `403` |
| 未知记录 / 版本 / 申请 | `404` |
| 受控记录直接发布、非受控记录提交申请、对已终结申请再操作、令牌到期时间非法 | `409` / `422` |

## 设计要点

- **不可变快照**：发布时把脱敏结果写入 `snapshots`（按 `(record_id, version)` 唯一），公开读取只访问快照；后续修订写入新版本，旧快照不变。
- **并发安全**：
  - 修订用条件更新 `UPDATE ... WHERE current_version = :base_version`，原子比较并递增，并发修订只有一个成功，其余收到 `409` 与当前版本；
  - 发布用 `INSERT ... ON CONFLICT DO NOTHING` 保证同一版本只有一份快照，快照与令牌同事务提交，不存在部分发布；
  - 受控复核用 `SELECT ... FOR UPDATE` 锁定申请行串行化并发批准/驳回，配合终态检查保证同一申请只有一个终态；
  - 公开读取的**有效性判定与次数消耗合并为同一条条件 `UPDATE ... WHERE 未撤销 AND 未到期 AND (不限次 OR reads_used < max_reads)`**，命中行即 `reads_used = reads_used + 1`：行锁串行化并发读取，成功次数绝不超过上限；撤销/到期不命中因而不消耗次数，且优先于“尚有剩余次数”；
  - 撤销是单条原子 `UPDATE`，提交后新开始的读取即被拒绝。
- **不泄露原文**：公开接口只返回快照中的脱敏正文；令牌无效/撤销/到期/次数用尽/与快照不匹配统一返回模糊的 `403 snapshot unavailable`；维护侧令牌查询与受控申请查询只返回元数据与计数，错误信息不含原文内容。
- **令牌**：`secrets.token_urlsafe(32)` 随机生成；支持 `expires_in_seconds` 或 `expires_at`（二选一）设置到期时间，并支持可选正整数 `max_reads` 读取次数上限；到期、撤销或次数用尽后读取一律拒绝。直接发布与受控批准共用同一套令牌规则、计数、查询与撤销接口。
- **受控发布**：
  - 申请行保存**冻结副本** `frozen_text`/`frozen_intervals`，复核批准基于副本做脱敏，历史版本行即使变化也不影响待审内容；
  - 批准/驳回先 `SELECT ... FOR UPDATE` 锁定申请行，把并发复核串行化：先提交者写入终态，后提交者看到非 `pending` 收到 `409`，配合 `status` 检查约束，保证同一申请只有一个终态；
  - 批准时**快照、新令牌、申请终态在同一事务提交**，任一异常整体回滚，不会留下无主或可读令牌；驳回不产生快照/令牌；
  - 个人凭据（HTTP Basic）与机构服务密钥（`X-Service-Key`）分离，角色为 `operator`/`reviewer`，申请人与复核人身份按用户名强制不同；
  - 既有数据库升级时启动会幂等补列 `records.controlled`（旧记录默认非受控）、`read_tokens.max_reads`/`reads_used`（既有令牌 `max_reads` 为空即继续不限次、已用为 0）与 `publish_requests.token_max_reads`，新表随 `create_all` 幂等创建。

## 本地开发

```bash
pip install -r requirements.txt
export SERVICE_KEY=dev-service-key-change-me
export DATABASE_URL=postgresql+psycopg2://redact:redact@localhost:5432/redact
# 可选：配置受控发布操作者（不配置则受控接口一律 401，其他功能不受影响）
export REVIEW_CREDENTIALS='[{"username":"alice","password":"alice-pw","roles":["operator"]},{"username":"bob","password":"bob-pw","roles":["reviewer"]}]'
uvicorn app.main:app --reload --port 8000

# 脱敏核心逻辑单元测试（仅依赖标准库）
python3 -m unittest tests.test_redaction -v
# 凭据配置校验单元测试
python3 -m unittest tests.test_config -v
# 受控发布端到端集成测试：默认用 pgserver 内置 PostgreSQL 自动建临时库，
# 也可用 TEST_DATABASE_URL 指向已有数据库
python3 -m unittest tests.test_integration_api -v
```

## 目录结构

```
app/
  main.py       # 路由与事务逻辑（受控发布申请-复核、令牌读取次数计数与查询）
  models.py     # records / record_versions / snapshots / read_tokens(含 max_reads/reads_used) / publish_requests
  redaction.py  # 区间校验、合并、替换（纯函数）
  schemas.py    # 请求/响应模型（含 max_reads 与令牌状态响应）
  security.py   # 服务密钥与操作者个人凭据（Basic）鉴权、角色依赖
  config.py     # 环境变量配置（含 REVIEW_CREDENTIALS 校验）
  db.py         # 引擎与会话
examples/
  demo.sh             # 直接发布端到端调用示例
  demo_controlled.sh  # 受控发布（申请-复核）端到端调用示例
tests/
  test_redaction.py       # 脱敏逻辑单元测试
  test_config.py          # 操作者凭据配置校验单元测试
  test_integration_api.py # 受控/既有接口端到端集成测试（真实 PostgreSQL）
Dockerfile / docker-compose.yml
```
