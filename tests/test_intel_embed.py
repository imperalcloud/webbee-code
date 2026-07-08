import numpy as np
import pytest
pytest.importorskip("model2vec")
from webbee.intel import embed


def test_model2vec_backend_embeds():
    b = embed.load_backend()
    assert b is not None and b.dim > 0 and b.model_id
    m = b.embed(["def alpha(): return 1", "class Foo: pass"])
    assert m.shape == (2, b.dim) and m.dtype == np.float32
    q = b.embed_query("where is alpha defined")
    assert q.shape == (b.dim,)


def test_load_backend_none_when_absent(monkeypatch):
    # force both importers to fail -> None (fallback path)
    monkeypatch.setattr(embed, "_try_model2vec", lambda: None)
    monkeypatch.setattr(embed, "_try_fastembed", lambda: None)
    assert embed.load_backend() is None
