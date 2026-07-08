from __future__ import annotations
import sys
import platform
from typing import Protocol

import numpy as np

_MODEL2VEC_MODEL = "minishlab/potion-base-8M"     # 256-dim, pure-numpy static
_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"        # 384-dim, ONNX


class EmbedBackend(Protocol):
    dim: int
    model_id: str
    def embed(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


class Model2VecBackend:
    def __init__(self, model) -> None:
        self._m = model
        self.model_id = f"model2vec:{_MODEL2VEC_MODEL}"
        self.dim = int(model.dim) if hasattr(model, "dim") else int(model.encode(["x"]).shape[1])

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self._m.encode(list(texts)), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(self._m.encode([text]), dtype=np.float32)[0]


class FastEmbedBackend:
    def __init__(self, model) -> None:
        self._m = model
        self.model_id = f"fastembed:{_FASTEMBED_MODEL}"
        self.dim = int(next(iter(model.embed(["x"]))).shape[0])

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(list(self._m.embed(list(texts))), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return np.asarray(next(iter(self._m.query_embed([text]))), dtype=np.float32)


def _try_model2vec():
    try:
        from model2vec import StaticModel
        return Model2VecBackend(StaticModel.from_pretrained(_MODEL2VEC_MODEL))
    except Exception:
        return None


def _fastembed_unsupported() -> bool:
    # PyPI-verified: onnxruntime has no wheel satisfying cp314 AND macOS-x86_64.
    return (sys.version_info >= (3, 14)
            and sys.platform == "darwin" and platform.machine() in ("x86_64", "i386"))


def _try_fastembed():
    if _fastembed_unsupported():
        return None
    try:
        from fastembed import TextEmbedding
        return FastEmbedBackend(TextEmbedding(model_name=_FASTEMBED_MODEL))
    except Exception:
        return None


def load_backend():
    """model2vec (pure-numpy, installs everywhere) preferred; fastembed opt-in;
    None -> the vector arm is disabled and search_code falls back to lexical+graph."""
    return _try_model2vec() or _try_fastembed()
