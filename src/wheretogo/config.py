"""全局配置（pydantic-settings，环境变量前缀 WTG_）。

数据库默认指向 Docker 隔离实例（端口 5433），绝不指向本地现有 PostgreSQL 生产实例。
"""
from __future__ import annotations

from datetime import timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 业务时区（PRD/活动时间一律 Asia/Shanghai 录入；落库 TIMESTAMPTZ 存 UTC）
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WTG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # —— 数据库（隔离实例）——
    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_user: str = "wheretogo"
    db_password: str = "wheretogo_dev_pwd"
    db_name: str = "wheretogo"
    db_langgraph_schema: str = "langgraph"

    # —— Redis（隔离实例）——
    redis_host: str = "127.0.0.1"
    redis_port: int = 6380
    redis_db: int = 0

    # —— 检索/模型（DD-05）——
    # use_real_models：是否优先调 API 模型（embedding/rerank）。无对应 key 时自动退确定性兜底。
    use_real_models: bool = False
    embedding_model: str = "BAAI/bge-m3"
    embedding_version: str = "bge-m3-v1"
    embedding_dim: int = 1024
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # —— 外部 Provider key（DD-04；BYO key，留空→确定性兜底，离线可测）——
    amap_key: str = ""
    qweather_key: str = ""
    variflight_key: str = ""
    search_provider: str = "bocha"  # bocha|tavily|exa|serper
    search_api_key: str = ""
    # LLM（OpenAI 兼容端点；模型中立）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model_default: str = "gpt-4o-mini"
    llm_use_routes: bool = False  # True=LLM_ROUTES 分层路由(面向 Qwen/DashScope)；False=全部用 llm_model_default（通用 OpenAI 端点）
    # embedding（OpenAI 兼容 /embeddings；留空→复用 LLM 的 base/key）
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_api_model: str = "text-embedding-3-small"
    embedding_pass_dimensions: bool = True  # 发 dimensions 参数(OpenAI text-embedding-3-* 支持；BGE-M3 兼容端点可关)
    # rerank（可选，DashScope gte-rerank；OpenAI 无 rerank，留空→确定性 Lexical 兜底）
    dashscope_api_key: str = ""
    rerank_api_model: str = "gte-rerank"

    # —— 深度研究（DD-17 §5b；全走 .env，项目可自定义）——
    deep_research_enabled: bool = True
    deep_research_max_rounds: int = 3
    deep_research_time_budget_s: int = 300
    deep_research_max_subagents: int = 4
    deep_research_concurrency: int = 8
    deep_research_cache_ttl_s: int = 300
    deep_research_min_coverage: float = 0.8
    # 候选语义评审通常需要处理一批带证据的候选；独立配置，避免受通用 LLM
    # 短调用默认超时影响。环境变量：WTG_DEEP_RESEARCH_SEMANTIC_JUDGE_TIMEOUT_S。
    deep_research_semantic_judge_timeout_s: float = 600.0
    deep_research_semantic_judge_batch_size: int = 10
    deep_research_semantic_judge_concurrency: int = 4
    # Source pages are summarized in parallel, but one extraction call can
    # legitimately contain several long snippets.  Keep this separate from the
    # generic provider timeout so deep research does not discard useful sources
    # after the old 60-second ceiling.
    deep_research_candidate_extract_timeout_s: float = 180.0
    trip_response_compose_timeout_s: float = 600.0
    chat_plan_lock_timeout_s: int = 600

    # —— v4 回合状态机与任务生命周期（Turn/Run/Outbox/Worker）——
    agent_v4_enabled: bool = True
    agent_outbox_poll_interval_s: float = 1.0
    agent_run_heartbeat_interval_s: int = 10
    agent_run_stall_threshold_s: int = 120
    agent_run_max_retries: int = 2
    agent_events_poll_interval_s: float = 1.0

    # —— 提醒 / ICS / 对象存储（DD-13；无 key→stub/本地兜底）——
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    resend_api_key: str = ""  # Email；留空→no-op stub
    ics_token_secret: str = "wheretogo-ics-dev-secret"  # 签发 calendar.ics 令牌（有默认）
    # 对象存储（截图）；留空→本地 uploads/ 兜底
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""

    # —— 应用 ——
    app_env: str = "dev"
    log_level: str = "INFO"

    @property
    def sync_db_url(self) -> str:
        """SQLAlchemy / Alembic 用同步 URL（psycopg3 驱动）。"""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def psycopg_dsn(self) -> str:
        """psycopg 原生 DSN（LangGraph PostgresSaver 用）。"""
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
