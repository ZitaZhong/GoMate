"""DD-05 检索服务（混合检索 + 重排）。"""

from .providers import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    LexicalRerankProvider,
    RerankProvider,
    get_embedding_provider,
    get_rerank_provider,
)
from .rrf import rrf
from .service import ActivityCandidate, DiningCandidate, RetrievalService, Weekend

__all__ = [
    "RetrievalService",
    "Weekend",
    "ActivityCandidate",
    "DiningCandidate",
    "rrf",
    "EmbeddingProvider",
    "RerankProvider",
    "HashingEmbeddingProvider",
    "LexicalRerankProvider",
    "get_embedding_provider",
    "get_rerank_provider",
]
