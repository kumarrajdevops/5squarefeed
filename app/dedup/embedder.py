"""
Sentence embeddings for the historical duplicate detector.

fastembed runs BAAI/bge-small-en-v1.5 through ONNX Runtime on CPU (no torch, no GPU, no service,
no vector database). The model downloads once into settings.embedding_cache_dir; after that the
detector works offline. Vectors are additionally cached on disk by text hash, so a story is
embedded once ever, not once per daily run.
"""
import hashlib
import os
from pathlib import Path
from typing import Protocol

import numpy as np

from app.config import settings


class EmbedderUnavailable(RuntimeError):
    """fastembed is not installed or the model cannot be loaded (e.g. first run without network)."""


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray:
        """L2-normalised vectors, one row per text."""


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class FastEmbedder:
    def __init__(self, model_name: str | None = None, cache_dir: str | None = None):
        self.name = model_name or settings.embedding_model
        self._cache_dir = cache_dir or settings.embedding_cache_dir
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding

                Path(self._cache_dir).mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(model_name=self.name, cache_dir=self._cache_dir)
            except Exception as exc:  # ImportError, download failure, corrupt cache
                raise EmbedderUnavailable(f"embedding model {self.name!r} unavailable: {exc}") from exc
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        vectors = np.array(list(self._load().embed(texts)), dtype=np.float32)
        return _normalise(vectors)


class CachedEmbedder:
    """Wraps an Embedder with an on-disk vector cache keyed by sha1(model + text). The cache is a
    plain .npz next to the model; a missing, unreadable or concurrently-written file only costs
    re-embedding, never a wrong vector (the key includes the model name and the full text)."""

    def __init__(self, inner: Embedder, cache_path: str | None = None, persist: bool = True):
        self.inner = inner
        self.name = inner.name
        self.persist = persist
        self.cache_path = Path(cache_path or Path(settings.embedding_cache_dir) / "vectors.npz")
        self._vectors: dict[str, np.ndarray] = {}
        self._dirty = False
        self.hits = 0
        self.computed = 0
        self._load_cache()

    def _key(self, text: str) -> str:
        return hashlib.sha1(f"{self.name}\x00{text}".encode("utf-8")).hexdigest()

    def _load_cache(self) -> None:
        if not (self.persist and self.cache_path.exists()):
            return
        try:
            with np.load(self.cache_path, allow_pickle=False) as data:
                self._vectors = {k: data[k] for k in data.files}
        except Exception:
            self._vectors = {}

    def embed(self, texts: list[str]) -> np.ndarray:
        keys = [self._key(t) for t in texts]
        missing = list(dict.fromkeys(k for k in keys if k not in self._vectors))
        if missing:
            by_key = dict(zip(keys, texts))
            computed = self.inner.embed([by_key[k] for k in missing])
            for key, vec in zip(missing, computed):
                self._vectors[key] = vec
            self._dirty = True
            self.computed += len(missing)
        self.hits += len(keys) - len(missing)
        if not keys:
            return np.zeros((0, 1), dtype=np.float32)
        return np.stack([self._vectors[k] for k in keys])

    def save(self) -> None:
        if not (self.persist and self._dirty):
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + f".{os.getpid()}.tmp")
            with open(tmp, "wb") as fh:
                np.savez(fh, **self._vectors)
            os.replace(tmp, self.cache_path)
            self._dirty = False
        except OSError:
            pass  # a cache that cannot be written only costs recomputation


def default_embedder(persist: bool = True) -> CachedEmbedder:
    return CachedEmbedder(FastEmbedder(), persist=persist)
