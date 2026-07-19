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


def test_add_over_mmap_loaded_matrix_is_copy_on_write(tmp_path):
    # F-Task15-followup: load_vectors' mmap_mode="r" perf win (Task 15)
    # returns a READ-ONLY array in the common case (on-disk dtype already
    # float32). IntelService.apply_changes' incremental re-embed path calls
    # add() for a chunk id that keeps its span (same id) but changed
    # content -- that hits add()'s in-place row assignment, which used to
    # raise "ValueError: assignment destination is read-only" against the
    # mmap. add() must copy-on-write exactly once and proceed; a subsequent
    # search must reflect the NEW vector, not the stale cached one.
    from webbee.intel import store

    cache = str(tmp_path / "c")
    ids = ["a.py#1-2", "b.py#1-2"]
    mat = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
    store.save_vectors(cache, "rk", "gitA", "model", ids, mat)
    got_ids, got_mat = store.load_vectors(cache, "rk", "gitA", "model")
    assert not got_mat.flags.writeable          # sanity: this IS the read-only mmap path

    vs = VectorStore.from_arrays(3, got_ids, got_mat)
    vs.add(["a.py#1-2"], np.array([0, 0, 1], dtype=np.float32))   # must not raise

    hits = vs.search(np.array([0, 0, 1], dtype=np.float32), top_n=1)
    assert hits[0][0] == "a.py#1-2" and hits[0][1] > 0.99   # reflects the NEW vector
