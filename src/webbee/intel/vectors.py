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
        """Insert new ids and/or overwrite the rows of ids already present.

        ORDER MATTERS: appends are materialised into `_mat` FIRST, overwrites
        are applied only afterwards. Doing it the other way round (the pre-
        2026-07-25 shape) wrote `_mat[self._pos[_id]]` while the rows for this
        very batch were still sitting in a pending `rows` list, so any id
        whose position had already been assigned but not yet vstacked indexed
        past the end of the array -- on a fresh store that is `_mat` with
        shape (0, dim) and the write raised IndexError. `IntelService.
        _embed_build` calls this once with the whole repo's chunk set and
        `boot.start_intel` swallows exceptions, so that IndexError silently
        disabled code intel for the entire session.

        A duplicate id WITHIN one batch is now also handled coherently: the
        first occurrence appends, later ones overwrite that same row (last
        value wins) instead of exploding.
        """
        vecs = np.asarray(vecs, dtype=np.float32).reshape(-1, self.dim)
        rows = []
        updates = []            # (row_index, vec) -- applied after the append
        for i, _id in enumerate(ids):
            pos = self._pos.get(_id)
            if pos is None:
                self._pos[_id] = len(self._ids)
                self._ids.append(_id)
                rows.append(vecs[i])
            else:
                updates.append((pos, vecs[i]))
        if rows:
            new_rows = np.array(rows, dtype=np.float32)
            self._mat = (np.vstack([self._mat, new_rows])
                         if self._mat.shape[0] else new_rows)
        if updates:
            if not self._mat.flags.writeable:
                # Copy-on-write: _mat may be a read-only mmap loaded straight
                # off the vector cache (store.load_vectors' mmap_mode="r" perf
                # win) via from_arrays. A read-only boot+search session never
                # reaches this branch; only an incremental re-embed of an
                # existing chunk id (same id, changed content) needs to mutate
                # a row in place, so pay for exactly one full copy here, lazily.
                self._mat = np.array(self._mat)
            for pos, vec in updates:
                self._mat[pos] = vec

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
