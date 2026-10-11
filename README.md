## 1. 系统边界

### 问题理解与整体架构

PDF明确提到业务系统不关心供应商响应内容，只需要确认通知请求已被本服务可靠接收，不应同步等待不稳定的外部 API。因此系统分为两个职责：

1. API 校验并持久化请求，事务提交后立即返回 `202 Accepted`。
2. 独立 Worker 异步投递，临时失败时重试，并保留最终状态供查询。

```mermaid
flowchart LR
    Business[业务系统] -->|"提交HTTP通知"| API[FastAPI]
    API -->|"事务持久化"| DB[(PostgreSQL)]
    Worker[DeliveryWorker] -->|"领取到期任务"| DB
    Worker -->|"HTTP(S)"| Vendor[外部供应商]
    Worker -->|"更新结果"| DB
```

### 选择解决的问题

- 接收并持久化不同目标地址、method、headers 和 JSON body 的通知请求。
- 使用 `Idempotency-Key` 保证业务系统重复提交不会创建重复任务。
- API 与外部供应商解耦，供应商延迟不会阻塞业务系统。
- 多 Worker 并发安全领取任务。
- HTTP 超时、有限重试、指数退避。
- Worker 崩溃后的任务恢复。
- 查询任务状态、尝试次数和最后错误。
- 基础 SSRF 防护：目标 host allowlist、不跟随重定向、拒绝危险 headers。
- 不将供应商凭证和请求 body 写入日志。

### 明确不解决的问题及原因

- **恰好一次成功**：外部 HTTP 与本地数据库无法原子提交；2xx 也只能表示 HTTP 层接受。
- **供应商模板与字段映射 DSL**：题目没有提供具体供应商 schema，第一版不对未知协议进行猜测。
- **响应 body 消费**：需求明确业务系统不关心返回内容。
- **复杂调用方鉴权、多租户权限和入口限流**：假设由企业 API Gateway 负责。本服务仍负责与目标请求直接相关的 URL/Header 校验。
- **供应商级顺序保证**：题目没有顺序要求；强制串行会降低吞吐。如果业务要求顺序，需要按实体或分区键单独设计。
- **应用层请求体大小限制**：生产入口应由 API Gateway、Nginx 或 Envoy 在请求进入 Python 前限制。

### 不同供应商格式如何处理

在MVP版中，Worker 不理解业务字段，只负责保存并发送请求快照。因此同一套可靠性机制可以服务广告、CRM、库存等供应商。

另一种方案是让业务系统提交统一内部事件，由 Connector/Adapter 转换成供应商请求。这能集中管理 URL、密钥和字段映射，但也会引入统一事件模型、模板版本、字段缺失、转换错误及历史任务使用哪个版本等问题。

如果未来多个业务系统重复维护相同供应商格式，可以在MVP之上增加：

```text
内部业务事件
  -> Destination/Secret 配置
  -> 版本化 Adapter 或模板
  -> 渲染不可变 HTTP 请求快照
  -> 可靠投递层
```

Adapter 应在任务入队前完成渲染并记录模板版本，避免重试时因模板变化而发送不同内容。

服务返回 `202` 后就接管投递责任。Worker 可能在原业务请求结束很久后重试，因此必须能够重建同一份请求。如果只保存业务 ID 并在重试时回源查询：

- 原业务系统会重新成为可用性依赖。
- 业务数据可能已经变化，重试内容不再代表原事件。


headers 可能包含供应商密钥，body 也可能包含敏感数据。当前明文持久化只适合受控 MVP。生产版应让任务携带 `destination_id`，由 Worker 从 Vault/Secrets Manager 获取静态凭证，并增加字段加密、访问控制和数据保留策略。

## 2. 可靠性与失败处理

### 投递语义：至少一次

MVP提供的是**至少一次尝试投递**

存在无法消除的失败窗口：

```text
供应商已经成功处理
  -> Worker 尚未写入 delivered
  -> Worker 崩溃
  -> lease 到期后任务重新发送
```

因此供应商可能收到重复请求。调用方应为同一业务事件使用稳定的幂等键；供应商支持时，应将该键作为供应商幂等 header 透传。没有供应商配合，本服务无法单方面保证业务效果恰好一次。

### 提交幂等

API 对 `Idempotency-Key` 建立数据库唯一约束，并对 URL、method、规范化 headers 和 body 计算请求指纹：

- 相同 key、相同内容：返回原任务。
- 相同 key、不同内容：返回 `409 Conflict`。
- 并发提交相同 key：由数据库唯一约束处理竞态。


### 并发领取、Lease 与 Fencing Token

Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务，在短事务中写入：

```text
status = processing
locked_until = now + LEASE_SECONDS
claim_token = 本次领取的UUID
```

数据库事务提交后再发 HTTP，请求期间不持有行锁。

正常 `processing` 且 lease 未到期的任务不会被其他 Worker 领取。只有 `locked_until <= now` 时，系统才把它视为可能被崩溃 Worker 遗弃并重新领取。Lease 必须大于 HTTP timeout；对于耗时不确定的长任务，生产版应增加 heartbeat 续租。

完成任务时必须同时匹配任务 ID、`processing` 状态和 `claim_token`。旧 Worker 即使在 lease 过期后迟到完成，也无法覆盖新 Worker 的数据库状态。Fencing token 只能保护本地状态，不能撤回已经发送的外部请求。

### 失败分类与重试

- HTTP 2xx：标记为 `delivered`。
- 网络错误、408、425、429、5xx：指数退避加随机抖动后重试。
- 其他 4xx：通常是参数、鉴权或权限错误，直接进入 `dead`。
- 达到 `MAX_ATTEMPTS`：进入 `dead`，保留最后状态码和错误。

Worker 收到响应 headers 后即按状态码判断并关闭响应流，不下载供应商响应 body。

`attempt_count` 统计领取到的尝试预算，而不是已确认写入网络的次数。Worker 在发送前崩溃也可能消耗一次预算，这是为了保守地限制故障流量，避免超过配置上限。

### 外部系统长期不可用

供应商长期不可用时，任务按有限指数退避逐渐拉长重试间隔。达到 `MAX_ATTEMPTS` 后进入 `dead`，停止继续消耗系统和供应商资源。

MVP 保留最终错误供查询，但不实现自动 redrive。生产版应：

- 对队列延迟、连续失败和 `dead` 增长建立告警。
- 提供带权限和审计记录的人工 redrive。
- 在根因修复后再重放任务，避免无限重试掩盖永久错误。
- 对单个供应商增加并发配额、限流和熔断，避免其故障影响其他供应商。

## 3. 取舍与演进

### MVP使用postgresql而不是消息队列和celery worker

在低到中等流量假设下，PostgreSQL 已经是任务真相源。利用事务、唯一约束、`next_attempt_at` 和 `SKIP LOCKED`，可以同时解决可靠接收、幂等、延迟调度和并发领取。

优点：

- API 的“保存任务”和“入队”是同一数据库事务。
- 不需要额外部署和监控 Redis/RabbitMQ。
- 任务状态容易查询和排障。
- 避免第一版出现 PostgreSQL 与消息队列双写不一致。

trade off：

- Worker 需要轮询数据库。
- 频繁状态更新会产生写放大和表膨胀。
- Worker 数量与任务规模增长后会增加数据库压力。
- 不具备专业 Broker 的路由、优先级和消费生态。

不使用 PostgreSQL 队列时，可以选择 RabbitMQ 或托管 SQS；但如果数据库仍保存查询状态，就必须处理数据库记录和消息发布的一致性。

当前每个 Worker 进程串行处理任务，优点是并发和故障模型简单，缺点是吞吐受供应商响应时间限制。可以先通过多个进程并行：

```bash
docker compose up --scale worker=4
```

多个 Worker 依靠 `SKIP LOCKED` 领取不同任务。进一步增长时可增加批量领取、进程内有限异步并发和按 host 的并发配额。

### AI 方案中没有采纳的过度设计

- Celery + Redis/RabbitMQ。
- Kafka 或事件流平台。
- 微服务拆分。
- 供应商模板 DSL 和虚构 Adapter。
- 管理后台和自动 redrive。
- 无限重试。


更完整的 AI 使用与取舍记录见 [AI_USAGE.md](AI_USAGE.md)。

### 流量或复杂度增长时如何演进

1. **单 Worker 吞吐不足**：增加 Worker 实例。
2. **单供应商故障影响全局**：增加按 host 限流、并发隔离和熔断。
3. **任务流量、路由、优先级和类型增加**：引入 RabbitMQ + Celery。
4. **引入独立 Broker**：同时实现 Transactional Outbox 和幂等消费。
5. **供应商格式重复维护**：增加 Destination Registry、Secret Manager 和版本化 Adapter。
6. **多租户出现**：将唯一约束演进为 `(tenant_id, idempotency_key)`，并增加租户授权和配额。

### 引入 RabbitMQ/Celery 后的一致性

因为 PostgreSQL 和 RabbitMQ 不能共享一个本地事务。推荐使用 Transactional Outbox：

```text
API 在同一 PostgreSQL 事务写 deliveries + outbox
  -> Outbox Publisher 发布 RabbitMQ 并等待 Publisher Confirm
  -> Celery Worker 按 delivery_id 幂等消费
  -> 调用供应商并更新状态
```

Publisher 可能在 RabbitMQ 已确认、数据库尚未标记已发布时崩溃，因此消息仍可能重复。Outbox 保证的是不丢消息和最终一致，不是绝对无重复；消费者和供应商仍需幂等。


## 4. API 与配置

### API

`POST /v1/deliveries`

- 必须提供 1–128 字符的 `Idempotency-Key`。
- method 允许 `POST`、`PUT`、`PATCH`。
- 成功持久化返回 `202`。
- 同一幂等键和相同内容返回原任务；同一键但不同内容返回 `409`。
- `Host`、`Content-Length` 和 hop-by-hop headers 会被拒绝。

`GET /v1/deliveries/{id}`

- 返回状态、尝试次数、下次尝试时间和最后错误。

`GET /health/live` 与 `GET /health/ready`

- 分别用于进程存活和数据库就绪检查。

### 关键配置（MVP中不需要修改任何东西）

- `DATABASE_URL`：SQLAlchemy PostgreSQL 连接串。
- `ALLOWED_HOSTS`：JSON 数组，例如 `["httpbin.org","example.com"]`；支持 `*.example.com`。
- `REQUEST_TIMEOUT_SECONDS`：HTTP 连接、写入和读取间隔超时，默认 10 秒。
- `LEASE_SECONDS`：Worker 租约，默认 30 秒，必须大于请求超时。
- `MAX_ATTEMPTS`：包含首次请求的最大尝试预算，默认 8。
- `RETRY_BASE_SECONDS`：指数退避基数，默认 2 秒。
- `RETRY_MAX_SECONDS`：单次退避上限，默认 3600 秒。
- `WORKER_POLL_SECONDS`：无任务时的轮询间隔，默认 1 秒。


## 5. 项目启动

### Docker Compose

要求 Docker Compose v2：

```bash
docker compose up --build
```

Compose 会先等待 PostgreSQL 健康并执行 Alembic migration，再启动 API 和 Worker。

提交一条成功通知：

```bash
curl -i http://localhost:8000/v1/deliveries \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-registration-1001' \
  -d '{
    "target_url": "https://httpbin.org/status/204",
    "method": "POST",
    "headers": {"X-Source": "registration-service"},
    "body": {"user_id": 1001, "event": "registered"}
  }'
```

使用响应中的 ID 查询：

```bash
curl http://localhost:8000/v1/deliveries/<delivery_id>
```

查看 Worker 和数据库：

```bash
docker compose logs -f worker
docker compose exec postgres psql -U callback -d callback \
  -c "SELECT id,status,attempt_count,last_status_code,last_error FROM deliveries ORDER BY created_at DESC;"
```

### 不使用 Docker

需要已有可连接的 PostgreSQL：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn app.api:app --reload
```

另一个终端启动 Worker：

```bash
. .venv/bin/activate
python -m app.worker
```

## 6. 测试

快速测试：

```bash
. .venv/bin/activate
make lint
make test
```

默认测试使用 SQLite 隔离验证 API 幂等、输入校验、失败分类、重试上限、lease 恢复和 fencing token。真正的并发领取依赖 PostgreSQL 语义。

运行 PostgreSQL 集成测试：

```bash
docker compose up -d postgres
docker compose run --rm migrate
TEST_DATABASE_URL=postgresql+psycopg://callback:callback@localhost:5432/callback \
  pytest tests/test_postgres_integration.py -v
```

验证临时失败与重试：

```bash
curl -i http://localhost:8000/v1/deliveries \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: e2e-retry-1' \
  -d '{
    "target_url": "https://httpbin.org/status/503",
    "method": "POST",
    "body": {"event": "retry-test"}
  }'
```

不设置 `TEST_DATABASE_URL` 时，只跳过 PostgreSQL 集成用例。
