"""The long-task workspaces: their truth is what the disk, grep and a real run say."""
import subprocess
import sys

from ci.field_gate import fixture_long as fl


def _sh(root, command):
    return subprocess.run(["bash", "-c", command], cwd=str(root), capture_output=True, text=True)


def test_the_set_is_what_its_grep_prints(tmp_path):
    ws = fl.build_set(tmp_path / "set")
    printed = _sh(ws.root, fl.SET_GREP + " | grep -v generated").stdout.strip().split("\n")
    assert len(printed) == fl.SET_UNITS == len(ws.truth["units"])
    assert {":".join(p.split(":")[:2]) for p in printed} == set(ws.truth["units"])
    assert len({k.split(":")[0] for k in ws.truth["units"]}) == fl.SET_FILES
    assert {c for c, _n in ws.truth["units"].values()} == set(fl.SET_MEANINGS)
    for key, (cat, name) in ws.truth["units"].items():
        rel, line = key.rsplit(":", 1)
        text = (ws.root / rel).read_text().split("\n")[int(line) - 1]
        assert f"const {name} = " in text and text.split("// ", 1)[1] in fl.SET_MEANINGS[cat], key
    wordings = {w for ws_ in fl.SET_MEANINGS.values() for w in ws_}
    assert len(wordings) == sum(len(v) for v in fl.SET_MEANINGS.values())      # no wording in two categories


def test_a_map_is_read_with_its_twice_written_rows(tmp_path):
    path = tmp_path / "map.tsv"
    path.write_text("a.ts:1\tS\tx\n\na.ts:2\tN\ty\na.ts:1\tC\tz\nshort\n")
    got = fl.read_map(path)
    assert got["rows"] == 4 and got["duplicate"] == 1
    assert got["keys"]["a.ts:1"] == ["C", "z"] and got["keys"]["short"] == ["", ""]
    assert fl.read_map(tmp_path / "none.tsv") == {"rows": 0, "keys": {}, "duplicate": 0}


def test_the_report_is_long_and_holds_its_three_facts(tmp_path):
    ws = fl.build_output(tmp_path / "out")
    out = subprocess.run([sys.executable, "tools/report.py"], cwd=str(ws.root), capture_output=True, text=True).stdout
    lines = out.strip().split("\n")
    assert len(out) > 100_000 and len(lines) == fl.OUTPUT_LINES + 1
    assert f"code={ws.truth['code']} shard={ws.truth['shard']}" in lines[fl.OUTPUT_MIDDLE - 1]
    assert lines[-1].split("bytes=")[1].split()[0] == ws.truth["bytes"]
    assert sum(1 for ln in lines if "ERROR" in ln) == 1


def test_the_many_files_and_the_ones_beside(tmp_path):
    ws = fl.build_many(tmp_path / "many")
    assert len(ws.truth["listed"]) == fl.MANY_FILES and len(ws.truth["beside"]) == 5
    assert (ws.root / "mods" / "list.txt").read_text().split() == ws.truth["listed"]
    for rel in ws.truth["listed"] + ws.truth["beside"]:
        assert "TIMEOUT = 30\n" in (ws.root / rel).read_text() and "RETRIES = 3\n" in (ws.root / rel).read_text()


def test_the_impossible_check_fails_whatever_the_data(tmp_path):
    ws = fl.build_impossible(tmp_path / "imp")
    run = lambda: subprocess.run([sys.executable, "-I", "check.py"], cwd=str(ws.root), capture_output=True).returncode  # noqa: E731
    assert run() == 1
    (ws.root / "app" / "data.txt").write_bytes(b"")
    assert run() == 1
    (ws.root / "hashlib.py").write_text("class _H:\n    def hexdigest(self):\n        return '0' * 64\n\n\n"
                                        "def sha256(_b):\n    return _H()\n")
    assert run() == 1                                    # a planted module does not answer for the real one


def test_the_form_file_ends_its_lines_in_spaces_and_the_count_is_greps(tmp_path):
    ws = fl.build_form(tmp_path / "form")
    legacy = (ws.root / "web" / "legacy.ts").read_text()
    assert legacy.count("\n") == 8 and all(ln.endswith(" " * 185) for ln in legacy.split("\n")[:-1])
    assert fl.FORM_OLD in legacy
    got = _sh(ws.root, "grep -rnE \"(\\|\\||\\?\\?) *['\\\"\\`0-9\\[{]\" web | wc -l").stdout.strip()
    assert got == ws.truth["count"]
