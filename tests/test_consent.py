from webbee.consent import ConsentGate

def test_reads_free_in_all_modes():
    for m in ("default", "plan", "autopilot"):
        d = ConsentGate(m).evaluate("read_file")
        assert d.allow and not d.needs_prompt

def test_default_prompts_on_write_and_bash():
    g = ConsentGate("default")
    for t in ("write_file", "edit_file", "bash"):
        d = g.evaluate(t)
        assert d.allow and d.needs_prompt

def test_plan_denies_writes():
    g = ConsentGate("plan")
    assert not g.evaluate("write_file").allow
    assert not g.evaluate("bash").allow
    assert g.evaluate("grep").allow

def test_autopilot_no_prompt():
    g = ConsentGate("autopilot")
    d = g.evaluate("bash")
    assert d.allow and not d.needs_prompt
