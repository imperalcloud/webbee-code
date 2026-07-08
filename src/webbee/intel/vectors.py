from __future__ import annotations
import numpy as np


def _normalize(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (m / n).astype(np.float32)


class VectorStore:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self._ids: list[str] = []
        self._pos: dict[str, int] = {}
        self._mat = np.zeros((0, dim), dtype=np.float32)

    def add(self, ids, vecs) -> None:
        vecs = np.asarray(vecs, dtype=np.float32).reshape(-1, self.dim)
        rows = []
        for i, _id in enumerate(ids):
            if _id in self._pos:
                self._mat[self._pos[_id]] = vecs[i]
            else:
                self._pos[_id] = len(self._ids)
                self._ids.append(_id)
                rows.append(vecs[i])
        if rows:
            self._mat = np.vstack([self._mat, np.array(rows, dtype=np.float32)]) if len(self._ids) > len(rows) else np.array(rows, dtype=np.float32)

    def remove(self, ids) -> None:
        drop = {i for i in ids if i in self._pos}
        if not drop:
            return
        keep = [(i, _id) for i, _id in enumerate(self._ids) if _id not in drop]
        self._mat = self._mat[[i for i, _ in keep]] if keep else np.zeros((0, self.dim), dtype=np.float32)
        self._ids = [_id for _, _id in keep]
        self._pos = {_id: n for n, _id in enumerate(self._ids)}

    def search(self, qvec, top_n: int):
        if not self._ids:
            return []
        q = np.asarray(qvec, dtype=np.float32).reshape(self.dim)
        qn = q / (np.linalg.norm(q) or 1.0)
        sims = _normalize(self._mat) @ qn
        n = min(top_n, len(self._ids))
        idx = np.argpartition(-sims, n - 1)[:n]
        idx = idx[np.argsort(-sims[idx])]
        return [(self._ids[i], float(sims[i])) for i in idx]

    def ids(self):
        return list(self._ids)

    def to_arrays(self):
        return list(self._ids), self._mat.copy()

    @classmethod
    def from_arrays(cls, dim, ids, matrix):
        vs = cls(dim)
        vs._ids = list(ids)
        vs._pos = {_id: n for n, _id in enumerate(vs._ids)}
        vs._mat = np.asarray(matrix, dtype=np.float32).reshape(-1, dim)
        return vs
