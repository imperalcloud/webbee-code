"""The terminal writes down where it stands when it stops answering (2026-11-27).

Five times in one day the kernel sent a command and the terminal never answered; nothing told where it stood. A
thread outside the event loop watches whether the loop turns and how long the turn's stage has lasted, and writes
every thread's stack to a file once per stall."""
import asyncio
import os
import threading
import time

from webbee import stallwatch as sw


def test_the_verdict_is_a_fact_of_two_clocks():
    assert sw.verdict(100.0, 99.0, "", 0.0) == ""                                   # the loop turns, no stage
    assert sw.verdict(100.0, 99.0, "tool bash", 90.0) == ""                         # a stage of ten seconds
    assert "event loop has not turned for 20 s" in sw.verdict(100.0, 80.0, "", 0.0)
    assert "reading the stream" in sw.verdict(100.0, 80.0, "", 0.0)
    assert sw.verdict(1000.0, 999.5, "tool bash", 600.0) == "the stage \'tool bash\' has lasted 400 s"
    assert "tool bash" in sw.verdict(1000.0, 900.0, "tool bash", 990.0)             # a blocked loop names its stage


def test_a_dump_names_the_reason_and_every_thread(tmp_path):
    hold = threading.Event()
    t = threading.Thread(target=hold.wait, name="a-waiting-thread", daemon=True)
    t.start()
    try:
        path = sw.dump("the stage \'tool bash\' has lasted 400 s", str(tmp_path))
    finally:
        hold.set()
    text = open(path).read()
    assert path.startswith(str(tmp_path)) and os.path.basename(path).startswith("stall-")
    assert "tool bash" in text and "webbee " in text and "threads:" in text
    assert text.count("Thread 0x") + text.count("Current thread") >= 2 and "wait" in text


def test_only_the_newest_files_are_kept(tmp_path):
    for i in range(sw.KEPT + 5):
        open(os.path.join(str(tmp_path), "stall-20260101-0000%02d-1.txt" % i), "w").close()
    sw.dump("x", str(tmp_path))
    assert len(os.listdir(str(tmp_path))) == sw.KEPT


def test_a_blocked_loop_is_written_down_once_and_a_running_one_never(tmp_path, monkeypatch):
    monkeypatch.setitem(sw._state, "started", False)
    said = []

    async def main():
        loop = asyncio.get_running_loop()
        assert sw.ensure(loop, on_stall=lambda path, why: said.append((path, why)),
                         loop_s=0.3, stage_s=60.0, tick=0.05, directory=str(tmp_path)) is True
        assert sw.ensure(loop) is False                       # one watch a process
        await asyncio.sleep(0.5)                              # the loop turns: nothing is written
        assert said == []
        sw.mark("tool bash")
        time.sleep(0.9)                                       # the loop is BLOCKED (a sync wait on it)
        sw.mark("")
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert len(said) == 1 and "event loop has not turned" in said[0][1] and "tool bash" in said[0][1]
    text = open(said[0][0]).read()                            # the stack names the place that blocks the loop
    assert " in main" in text and "test_stallwatch.py" in text and "webbee-stall-watch" not in said[0][1]


def test_a_stage_that_lasts_too_long_is_written_down_while_the_loop_turns(tmp_path, monkeypatch):
    monkeypatch.setitem(sw._state, "started", False)
    said = []

    async def main():
        sw.ensure(asyncio.get_running_loop(), on_stall=lambda path, why: said.append(why),
                  loop_s=60.0, stage_s=0.3, tick=0.05, directory=str(tmp_path))
        sw.mark("tool read_file")
        await asyncio.sleep(0.8)                              # the loop turns, the stage does not end
        sw.mark("")
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert len(said) == 1 and said[0].startswith("the stage \'tool read_file\' has lasted")
