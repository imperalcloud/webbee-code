import numpy as np
from webbee.intel.vectors import VectorStore


def test_add_search_cosine():
    vs = VectorStore(dim=3)
    vs.add(["a", "b", "c"], np.array([[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0]], dtype=np.float32))
    hits = vs.search(np.array([1, 0, 0], dtype=np.float32), top_n=2)
    assert hits[0][0] == "a" and hits[1][0] == "c"        # nearest by cosine
    assert hits[0][1] > hits[1][1]


def test_remove():
    vs = VectorStore(dim=3)
    vs.add(["a", "b"], np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32))
    vs.remove(["a"])
    hits = vs.search(np.array([1, 0, 0], dtype=np.float32), top_n=5)
    assert [h[0] for h in hits] == ["b"]


def test_roundtrip_arrays():
    vs = VectorStore(dim=2)
    vs.add(["x", "y"], np.array([[1, 0], [0, 1]], dtype=np.float32))
    ids, mat = vs.to_arrays()
    vs2 = VectorStore.from_arrays(2, ids, mat)
    assert vs2.ids() == ["x", "y"]
