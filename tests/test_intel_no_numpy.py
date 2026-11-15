"""F2 regression: `pip install webbee[intel]` (no embed extra -- no numpy,
no model2vec) must leave intel FUNCTIONAL as lexical+graph, never crash
build() or degrade search_code to an error envelope. Simulated here by
forcing embed.load_backend() to raise (standing in for the real failure
mode: a bare `import numpy` inside embed.py/store.py with numpy absent)."""
import pytest
pytest.importorskip("tree_sitter")
from webbee.intel.service import IntelService
from webbee.intel import query
from imperal_sdk import code_index as embed_mod


