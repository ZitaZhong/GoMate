"""0006 v4 回合状态机与任务生命周期：Turn / Run / RunEvent / Clarification / Outbox。

- v4 §6.2：`agent_turns`（每个用户回合的持久化事务，含状态机与幂等键）
- v4 §6.4：`agent_runs`（研究/重排/回答/重规划的真实运行实例）
- v4 §6.5：`agent_run_events`（单调递增事件流，SSE Last-Event-ID 续传的事实源）
- v4 §6.3：`clarification_requests`（阻塞/非阻塞澄清，持久化可恢复）
- v4 §9.2：`agent_outbox`（原子提交 + Worker 领取，避免"承诺执行但任务未创建"）

全程 `IF NOT EXISTS` 幂等；只动隔离库 `wheretogo`；不新增 PG ENUM（用 TEXT + 应用枚举校验）。
"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # —— v4 §6.2 AgentTurn：一次对话回合 = 一条持久化事务 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_turns (
          id               UUID PRIMARY KEY,
          plan_id          BIGINT REFERENCES plans(id) ON DELETE CASCADE,
          sequence_no      INT NOT NULL,
          user_message     TEXT NOT NULL,
          status           TEXT NOT NULL DEFAULT 'received',  -- TurnStatus 应用枚举
          interpretation   JSONB DEFAULT '{}',                -- Interpreter 输出（脱敏）
          visible_reply    TEXT,
          clarification_id UUID,
          run_id           UUID,
          error_code       TEXT,
          client_key       TEXT,                              -- Idempotency-Key
          created_at       TIMESTAMPTZ DEFAULT now(),
          updated_at       TIMESTAMPTZ DEFAULT now(),
          completed_at     TIMESTAMPTZ,
          CONSTRAINT uq_agent_turns_plan_seq UNIQUE (plan_id, sequence_no)
        );
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_turns_plan_client_key "
        "ON agent_turns (plan_id, client_key) WHERE client_key IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_turns_plan_status ON agent_turns (plan_id, status);"
    )

    # —— v4 §6.4 AgentRun：真实、持久化、可查询的运行实例 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
          id               UUID PRIMARY KEY,
          plan_id          BIGINT REFERENCES plans(id) ON DELETE CASCADE,
          turn_id          UUID REFERENCES agent_turns(id) ON DELETE CASCADE,
          parent_run_id    UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
          run_type         TEXT NOT NULL,                     -- research/recompose/answer/replan
          status           TEXT NOT NULL DEFAULT 'queued',    -- RunStatus 应用枚举
          goal             TEXT NOT NULL DEFAULT '',
          execution_plan   JSONB DEFAULT '{}',
          required_inputs  JSONB DEFAULT '{}',
          assumptions      JSONB DEFAULT '[]',
          checkpoint_ref   TEXT,                              -- LangGraph thread_id
          result_bundle_id BIGINT,
          error_code       TEXT,
          retry_count      INT NOT NULL DEFAULT 0,
          cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
          created_at       TIMESTAMPTZ DEFAULT now(),
          started_at       TIMESTAMPTZ,
          heartbeat_at     TIMESTAMPTZ,
          completed_at     TIMESTAMPTZ
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_plan_status ON agent_runs (plan_id, status);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_status_heartbeat ON agent_runs (status, heartbeat_at);"
    )

    # —— v4 §6.5 RunEvent：UI 展示与问题追踪的统一事实源（可续传）——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_run_events (
          id         BIGSERIAL PRIMARY KEY,
          run_id     UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          sequence   INT NOT NULL,
          type       TEXT NOT NULL,                           -- research.progress/run.status/...
          phase      TEXT,                                    -- queued/planning/searching/...
          message    TEXT,
          payload    JSONB DEFAULT '{}',
          created_at TIMESTAMPTZ DEFAULT now(),
          CONSTRAINT uq_agent_run_events_seq UNIQUE (run_id, sequence)
        );
        """
    )

    # —— v4 §6.3 ClarificationRequest：阻塞/非阻塞澄清，刷新后仍可见 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS clarification_requests (
          id                    UUID PRIMARY KEY,
          turn_id               UUID NOT NULL REFERENCES agent_turns(id) ON DELETE CASCADE,
          question              TEXT NOT NULL,
          reason                TEXT DEFAULT '',
          blocking              BOOLEAN NOT NULL DEFAULT FALSE,
          requested_facts       JSONB DEFAULT '[]',
          assumptions_if_skipped JSONB DEFAULT '[]',
          status                TEXT NOT NULL DEFAULT 'open',  -- open/answered/skipped/expired
          answer_turn_id        UUID,
          created_at            TIMESTAMPTZ DEFAULT now(),
          updated_at            TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_clarification_turn_status "
        "ON clarification_requests (turn_id, status);"
    )

    # —— v4 §9.2 Outbox：Turn 事务内写入，Worker FOR UPDATE SKIP LOCKED 领取 ——
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_outbox (
          id         BIGSERIAL PRIMARY KEY,
          topic      TEXT NOT NULL,                            -- agent_run.requested
          payload    JSONB NOT NULL DEFAULT '{}',
          status     TEXT NOT NULL DEFAULT 'pending',          -- pending/claimed/done/failed
          attempts   INT NOT NULL DEFAULT 0,
          claimed_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ DEFAULT now(),
          updated_at TIMESTAMPTZ DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_outbox_status_created "
        "ON agent_outbox (status, created_at);"
    )


def downgrade() -> None:
    for tbl in (
        "agent_outbox",
        "clarification_requests",
        "agent_run_events",
        "agent_runs",
        "agent_turns",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
