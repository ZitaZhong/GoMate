"""BFF 应用（FastAPI + SSE）。运行：uvicorn wheretogo.bff.app:app --reload"""

from .app import app

__all__ = ["app"]
