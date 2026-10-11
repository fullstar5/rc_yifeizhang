# AI 使用说明

## AI 提供的帮助

- 将题目拆成系统边界、投递语义、失败策略、工程交付和演进五部分，并检查是否逐项回答了题目。
- 比较“PostgreSQL 作为持久队列”和 Celery/RabbitMQ/Kafka 等方案，形成首版架构草案。
- 提醒并补充容易遗漏的失败窗口：外部请求成功但写库前崩溃、worker lease 过期、旧 worker 覆盖新状态、幂等键并发竞争。
- 帮助比较自定义同步 worker 与 Celery/RabbitMQ，并说明生产环境引入 Broker 时需要 Transactional Outbox 和幂等消费。
- 生成 FastAPI、SQLAlchemy、Alembic、worker、Docker Compose 和测试的mvp代码，并运行测试和静态检查后修正问题。
- 帮助二次编写readme和AI usage、整理mvp的启动步骤、绘制时序图。

## 没有采纳或没有放进 MVP 的 AI 建议

- **直接引入 Celery/Redis、RabbitMQ 或 Kafka**：当前没有足以证明需要独立消息系统的吞吐与隔离需求；它们会增加部署和一致性处理。首版改用 PostgreSQL 的事务与 `SKIP LOCKED`。
- **提供管理后台和 dead-letter 自动重放**：第一版只保留可查询状态，并把受审计 redrive 作为演进项。
- **无限重试或长期持续重试**：会显著增加项目复杂度和资源消耗。采用有限次数后标记为 `dead`。
- **供应商Adapter/字段映射DSL**：需求允许业务方直接提交 headers/body。题目中缺少信息无法设计adapter，过早设计 DSL 会引入版本、校验和调试复杂度。
- **完整身份认证、租户授权和网络出口系统**：第一版不自行建设，分别交给企业 API Gateway 和基础设施层；应用仍保留与投递目标直接相关的 allowlist、危险 Header 和重定向校验。

## 由我做出的关键决策

- Python/FastAPI：因为这是MVP因此希望在极短时间内设计出可靠交付、测试的技术栈，而不是为了追求最高理论吞吐。
- PostgreSQL队列方案：因为首版最重要的是把“接收记录”和“任务入队”放在同一事务中，吞吐量和轮询性能限制的缺点在MVP中可以被接受。
- 选择承诺“至少一次”方案：不选择“恰好一次”。原因是外部 HTTP 请求与本地数据库无法原子提交，崩溃窗口客观存在，隐藏重复风险会误导调用方。
- 有限重试：区分 4xx 与临时错误，让长期故障最终可见。
- 单进程串行、可多实例扩展的自定义 worker：只有在吞吐或任务复杂度有明确指标时才引入 Celery/RabbitMQ。
- 我决定把 host allowlist、禁止危险 headers 和不跟随重定向放入MVP，但选择省略其余安全校验和网关实现。
