"""Local CPU embeddings (ONNX via fastembed). Zero Claude tokens.

Model: intfloat/multilingual-e5-small (384 dim, ~470 MB). Multilingual (Spanish/English), trained for
asymmetric query->passage retrieval; it requires the "query: " and "passage: " prefixes.
"""
from __future__ import annotations

import threading
from typing import Iterable, Protocol

import numpy as np


class Embedder(Protocol):
    dim: int

    def passages(self, texts: list[str]) -> list[np.ndarray]: ...

    def query(self, text: str) -> np.ndarray: ...


class E5Embedder:
    def __init__(self, model: str, cache_dir: str, dim: int = 384, threads: int = 4):
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType
        known = {m["model"] for m in TextEmbedding.list_supported_models()}
        if model not in known:
            TextEmbedding.add_custom_model(
                model=model, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=model), dim=dim, model_file="onnx/model.onnx")
        self.model = TextEmbedding(model, cache_dir=cache_dir, threads=threads)
        self.dim = dim
        self._lock = threading.Lock()

    def _embed(self, texts: Iterable[str]) -> list[np.ndarray]:
        with self._lock:
            return [np.asarray(v, dtype=np.float32) for v in self.model.embed(list(texts), batch_size=32)]

    def passages(self, texts: list[str]) -> list[np.ndarray]:
        return self._embed("passage: " + t for t in texts)

    def query(self, text: str) -> np.ndarray:
        return self._embed(["query: " + text])[0]


class HashEmbedder:
    """Deterministic embedder for tests (hashed bag of words). Not for production."""

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _one(self, t: str) -> np.ndarray:
        import hashlib
        import re
        v = np.zeros(self.dim, dtype=np.float32)
        for w in re.findall(r"\w+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def passages(self, texts):
        return [self._one(t) for t in texts]

    def query(self, text):
        return self._one(text)


_default: E5Embedder | None = None


def get_default(cfg) -> E5Embedder:
    global _default
    if _default is None:
        _default = E5Embedder(cfg.embedding_model, cfg.models_dir, cfg.embedding_dim)
    return _default
