"""embedding / rerank Provider（DD-05 §4、§6；经 DD-04 Provider 思路封装）。

v2 口径（与 LLM 同构——调 API，不用本地 torch）：
  - 优先 **DashScope/通义千问 API**（`text-embedding` / `gte-rerank`，BYO `WTG_DASHSCOPE_API_KEY`）；
  - 无 key 或调用失败 → **确定性 fallback**（字/双字 n-gram 特征哈希 embedding + n-gram 余弦重排），
    使检索管线在无 key/无 GPU 环境下也能被有意义地测试；配齐 key 即平滑升级为真实质量。
  - 本地 BGE-M3（FlagEmbedding+torch）保留为**已弃用**的可选路径（`--extra models`），不再默认。

fallback 不是“假随机”，而是真实（轻量）的词法信号——共享 n-gram 越多相似度越高。
"""
from __future__ import annotations

import hashlib
import logging
import math
from abc import ABC, abstractmethod
from collections import Counter

import numpy as np

from ..config import get_settings

logger = logging.getLogger(__name__)

#: Hashing 兜底向量的版本标识（写 activities.embedding_version，如实标注，不冒名 API 模型）
HASHING_EMBEDDING_VERSION = "hashing-ngram-v1"


def ngrams(text: str) -> list[str]:
    """字 unigram + bigram（去空白、小写）。对中文按字切分，天然适配无分词场景。"""
    t = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    if not t:
        return []
    grams = list(t)
    grams += [t[i : i + 2] for i in range(len(t) - 1)]
    return grams


# ============================ embedding ============================
class EmbeddingProvider(ABC):
    available: bool = True
    dim: int = 1024
    #: 本 provider 产出向量的版本标识（写 activities.embedding_version，如实标注）
    version: str = "unknown"

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashingEmbeddingProvider(EmbeddingProvider):
    """signed feature hashing embedding（确定性、无需权重）。"""

    available = True
    version = HASHING_EMBEDDING_VERSION

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or get_settings().embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        toks = ngrams(text)
        if not toks:
            vec[0] = 1.0
            return vec.tolist()
        for tok in toks:
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[idx] += sign
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            vec[0] = 1.0
            norm = 1.0
        vec /= norm
        return vec.tolist()


class BGEM3EmbeddingProvider(EmbeddingProvider):
    """【已弃用·可选】BGE-M3 稠密向量（本地 torch）。需 `--extra models` 与权重。

    v2 默认改走 DashScope API（见 DashScopeEmbeddingProvider）。仅在用户显式希望本地自托管、
    且未配置 DashScope key 时作为后备保留。
    """

    available = True

    def __init__(self) -> None:
        from FlagEmbedding import BGEM3FlagModel  # 懒加载，避免无模型环境导入失败

        s = get_settings()
        self.dim = s.embedding_dim
        self.version = s.embedding_version
        self._model = BGEM3FlagModel(s.embedding_model, use_fp16=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = self._model.encode(texts, return_dense=True, return_sparse=False)["dense_vecs"]
        return [list(map(float, v)) for v in out]


class DashScopeEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容 text-embedding API（`{embedding_base_url|llm_base_url}/embeddings`）。

    BYO `WTG_EMBEDDING_API_KEY`（留空→复用 `WTG_LLM_API_KEY`）。调用失败（无网络/限流/key 失效）
    → 内部退 `HashingEmbeddingProvider`（确定性），保证检索管线不阻断（DD-05 降级哲学）。
    连续失败 `_CIRCUIT_THRESHOLD` 次后熔断：本进程后续直接走哈希（不再每次白跑超时），
    且 `version` 如实标注 `hashing-ngram-v1`，不冒名 API 模型版本。
    """

    available = True
    _CIRCUIT_THRESHOLD = 3  # 连续失败 N 次 → 熔断
    _circuit_failures = 0   # 类级计数：进程内跨实例共享（熔断粒度=本进程）
    _circuit_open = False

    def __init__(self) -> None:
        s = get_settings()
        self.dim = s.embedding_dim
        self._api_key = s.embedding_api_key or s.llm_api_key
        self._model = s.embedding_api_model
        base = (s.embedding_base_url or s.llm_base_url).rstrip("/")
        self._url = f"{base}/embeddings"
        self._pass_dim = s.embedding_pass_dimensions
        self._fallback = HashingEmbeddingProvider(s.embedding_dim)
        self._degraded = False  # 最近一次 embed 是否实际走了哈希兜底

    @property
    def version(self) -> str:
        """实际产出向量的版本：熔断中/最近调用走了哈希兜底 → 如实标注 hashing 版本。"""
        if type(self)._circuit_open or self._degraded:
            return HASHING_EMBEDDING_VERSION
        return get_settings().embedding_version

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        cls = type(self)
        if cls._circuit_open:
            # 熔断中：直接走哈希兜底（degraded），不再白跑 30s 超时
            self._degraded = True
            return self._fallback.embed(texts)
        try:
            import httpx

            body: dict = {"model": self._model, "input": texts}
            if self._pass_dim:
                body["dimensions"] = self.dim  # OpenAI text-embedding-3-* 支持；否则关闭
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json().get("data", [])
                out = [list(map(float, d["embedding"])) for d in data]
            if len(out) != len(texts):  # 数量不符视为失败 → 兜底
                raise ValueError("embedding count mismatch")
        except Exception as exc:
            cls._circuit_failures += 1
            self._degraded = True
            if cls._circuit_failures >= cls._CIRCUIT_THRESHOLD and not cls._circuit_open:
                cls._circuit_open = True
                logger.warning(
                    "embedding API 连续失败 %d 次，熔断：本进程后续直接走哈希兜底"
                    "（degraded，embedding_version=%s）；最近错误：%s",
                    cls._circuit_failures, HASHING_EMBEDDING_VERSION, exc,
                )
            else:
                logger.warning(
                    "embedding API 调用失败（连续 %d 次），本次退哈希兜底（degraded）：%s",
                    cls._circuit_failures, exc,
                )
            return self._fallback.embed(texts)
        cls._circuit_failures = 0  # 成功 → 重置连续失败计数
        self._degraded = False
        return out


# ============================ rerank ============================
class RerankProvider(ABC):
    available: bool = True

    @abstractmethod
    def score(self, query: str, docs: list[str]) -> list[float]:
        ...


class LexicalRerankProvider(RerankProvider):
    """交叉打分（轻量）：query 与 doc 的 n-gram 计数余弦。确定性、无需权重。"""

    available = True

    def score(self, query: str, docs: list[str]) -> list[float]:
        q = Counter(ngrams(query))
        qn = math.sqrt(sum(v * v for v in q.values())) or 1.0
        out: list[float] = []
        for d in docs:
            dc = Counter(ngrams(d))
            if not dc:
                out.append(0.0)
                continue
            common = set(q) & set(dc)
            dot = sum(q[t] * dc[t] for t in common)
            dn = math.sqrt(sum(v * v for v in dc.values())) or 1.0
            out.append(dot / (qn * dn))
        return out


class BGEReranker(RerankProvider):
    """【已弃用·可选】BGE-reranker-v2-m3 本地交叉编码。需 `--extra models` 与权重。"""

    available = True

    def __init__(self) -> None:
        from FlagEmbedding import FlagReranker  # 懒加载

        self._model = FlagReranker(get_settings().reranker_model, use_fp16=True)

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        pairs = [[query, d] for d in docs]
        scores = self._model.compute_score(pairs, normalize=True)
        if isinstance(scores, float):
            scores = [scores]
        return [float(s) for s in scores]


class DashScopeRerankProvider(RerankProvider):
    """DashScope/通义千问 gte-rerank API（BYO `WTG_DASHSCOPE_API_KEY`，与 LLM 同构）。

    DashScope 原生 rerank 端点。调用失败 → 内部退 `LexicalRerankProvider`（确定性）。
    """

    available = True
    _RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

    def __init__(self) -> None:
        s = get_settings()
        self._api_key = s.dashscope_api_key
        self._model = s.rerank_api_model
        self._fallback = LexicalRerankProvider()

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        try:
            import httpx

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    self._RERANK_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "input": {"query": query, "documents": docs},
                        "parameters": {"return_documents": False, "top_n": len(docs)},
                    },
                )
                resp.raise_for_status()
                results = resp.json()["output"]["results"]  # [{index, relevance_score}]
            scores = [0.0] * len(docs)
            for item in results:
                idx = item["index"]
                if 0 <= idx < len(docs):
                    scores[idx] = float(item["relevance_score"])
            return scores
        except Exception:
            return self._fallback.score(query, docs)


# ============================ 工厂 ============================
def get_embedding_provider() -> EmbeddingProvider:
    """v2：优先 OpenAI 兼容 embedding API（use_real_models 且有 embedding/llm key）；否则 Hashing 兜底。"""
    s = get_settings()
    if s.use_real_models and (s.embedding_api_key or s.llm_api_key):
        return DashScopeEmbeddingProvider()  # 内部失败自降级到 Hashing
    return HashingEmbeddingProvider()


def get_rerank_provider() -> RerankProvider:
    """rerank 可选（DashScope gte-rerank）；无 key → 确定性 Lexical 兜底（OpenAI 无 rerank）。"""
    s = get_settings()
    if s.use_real_models and s.dashscope_api_key:
        return DashScopeRerankProvider()
    return LexicalRerankProvider()
