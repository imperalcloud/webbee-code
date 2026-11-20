"""The workspaces of the "work that can wait" scenarios are what their scenarios judge by."""
import subprocess
import sys

from ci.field_gate import fixture_wait as fw
from ci.field_gate.scenarios_wait import end_of


def test_the_end_line_is_read_from_what_the_client_printed():
    assert end_of("...\nend: question\n") == "question"
    assert end_of("end: answer\nmore\nend: brake\n") == "brake"
    assert end_of("⏸ Marathon paused: no_progress\n") == ""
    assert end_of("the end: question is open") == ""


def test_two_files_one_name_hold_the_same_id_for_two_people(tmp_path):
    ws = fw.build_same_name(tmp_path / "w")
    rows = [(ws.root / r).read_text().splitlines() for r in ws.truth["both"]]
    named = [next(ln for ln in f if ln.startswith("1042,")) for f in rows]
    assert len(set(named)) == 2 and fw.unchanged(ws, ws.truth["both"]) == []
    (ws.root / ws.truth["both"][0]).write_text("id,name,plan\n")
    assert fw.unchanged(ws, ws.truth["both"]) == [ws.truth["both"][0]]


def test_the_space_check_passes_only_without_the_export(tmp_path):
    ws = fw.build_act(tmp_path / "w")
    run = lambda: subprocess.run([sys.executable, "-I", "check_space.py"], cwd=str(ws.root),  # noqa: E731
                                 capture_output=True).returncode
    assert run() == 1
    (ws.root / "cache/pages.bin").unlink()
    assert run() == 1                                   # the cache alone is not enough
    (ws.root / ws.truth["export"]).unlink()
    assert run() == 0


def test_the_study_numbers_are_the_modules_own(tmp_path):
    ws = fw.build_study(tmp_path / "w")
    out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); from app.retry import "
                          "delay; print(delay(5), sum(delay(n) for n in range(1, 6)))"], cwd=str(ws.root),
                         capture_output=True, text=True).stdout.split()
    assert [float(x) for x in out] == [ws.truth["fifth"], ws.truth["total"]]


def test_the_heavy_task_names_a_config_that_exists_twice_and_a_host_that_never_resolves(tmp_path):
    ws = fw.build_ground(tmp_path / "w")
    assert all((ws.root / r).is_file() for r in ws.truth["beta"]) and fw.UNREACHABLE.endswith(".invalid")
    assert fw.UNREACHABLE in (ws.root / "services.txt").read_text()
