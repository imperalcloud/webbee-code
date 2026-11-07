"""Ignat 07.11 §4: 10 of 11 failures were edit_file on the agent's own results file with an anchor typed from
memory. Appending to the end needs no anchor."""
from webbee.tools import LocalToolExecutor, _WRITE_TIER


def test_append_adds_to_the_end_and_creates_the_file(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    r1 = ex.run("append_file", {"path": ".scratch/res.md", "content": "## Step 1\nok\n"})
    r2 = ex.run("append_file", {"path": ".scratch/res.md", "content": "## Step 2\nСБОЙ: x\n"})
    assert r1["ok"] and r2["ok"] and "2 lines" not in r2["content"]
    assert (tmp_path / ".scratch/res.md").read_text() == "## Step 1\nok\n## Step 2\nСБОЙ: x\n"


def test_append_starts_on_a_new_line(tmp_path):
    (tmp_path / "log.md").write_text("no newline at end")
    LocalToolExecutor(str(tmp_path)).run("append_file", {"path": "log.md", "content": "next"})
    assert (tmp_path / "log.md").read_text() == "no newline at end\nnext"


def test_append_is_a_write_and_stays_in_the_workspace(tmp_path):
    assert "append_file" in _WRITE_TIER
    out = LocalToolExecutor(str(tmp_path)).run("append_file", {"path": "../outside.md", "content": "x"})
    assert out["ok"] is False
