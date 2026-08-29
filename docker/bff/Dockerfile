# 「周末去哪儿」后端镜像
# Python 3.12 + uv + FastAPI BFF + Agent Worker
#
# 构建: docker build -f docker/bff/Dockerfile -t wheretogo-bff .
# 运行 BFF: docker run -p 8000:8000 wheretogo-bff
# 运行 Worker: docker run wheretogo-bff uv run python -m wheretogo.agent.worker

FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（psycopg 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv（快速 Python 包管理器）
RUN pip install --no-cache-dir uv

# 复制依赖文件
COPY pyproject.toml uv.lock ./

# 安装依赖（不含 dev 依赖）
RUN uv sync --no-dev --frozen

# 复制源代码和配置
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY seeds ./seeds

# 设置 Python 路径
ENV PYTHONPATH=/app/src

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health')" || exit 1

# 默认启动 BFF（可在 docker-compose 中覆盖为 worker）
CMD ["uv", "run", "uvicorn", "wheretogo.bff.app:app", "--host", "0.0.0.0", "--port", "8000"]
