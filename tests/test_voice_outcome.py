"""A voice recording reaches the agent as the person's words or not at all (live 2026-11-17 se268e3: a recording
nothing was recognised in was sent as "[Voice note: 7s audio -- audio recorded]" and became the task's message)."""
from webbee.modals import voice_outcome


def test_recognised_words_are_sent_as_they_are():
    assert voice_outcome("  move the posts to the right  ", 7) == ("move the posts to the right", "")


def test_a_recording_without_words_is_not_sent_and_the_person_is_told():
    for heard in ("", "   ", "you", "Thank you", "."):
        words, note = voice_outcome(heard, 7)
        assert words == "" and "nothing was sent" in note and "7s" in note


def test_a_failed_recognition_is_not_sent_and_is_told_apart():
    words, note = voice_outcome(None, 12)
    assert words == "" and "could not be recognised" in note and "nothing was sent" in note


# ── the reason a recording gave no words is told, and the token is the client's live one (2026-11-26) ──
def test_the_reason_is_told_in_plain_words():
    words, note = voice_outcome("", 5, "there are not enough credits for voice")
    assert words == "" and note == "🎙️ nothing was sent: there are not enough credits for voice."
    assert voice_outcome("hello", 5, "stale reason") == ("hello", "")


def test_an_empty_recording_names_the_microphone(tmp_path):
    import webbee.modals as m
    p = tmp_path / "v.wav"
    p.write_bytes(b"RIFF")
    assert m.transcribe_audio_file(str(p)) == "" and "microphone" in m.LAST_VOICE_WHY


def test_the_voice_request_carries_a_fresh_token_and_retries_once_when_it_was_rotated(tmp_path, monkeypatch):
    import io
    import json
    import urllib.error
    import urllib.request
    import webbee.modals as m
    p = tmp_path / "v.wav"
    p.write_bytes(b"x" * 2000)
    asked, sent = [], []
    monkeypatch.setattr(m, "_voice_token", lambda force=False: asked.append(force) or ("new" if force else "old"))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"text": " move the posts "}).encode()

    def _open(req, timeout=0):
        sent.append(req.get_header("Authorization"))
        if len(sent) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert m.transcribe_audio_file(str(p), api_url="https://x") == "move the posts"
    assert asked == [False, True] and sent == ["Bearer old", "Bearer new"] and m.LAST_VOICE_WHY == ""


def test_a_refusal_of_the_voice_service_is_named(tmp_path, monkeypatch):
    import io
    import urllib.error
    import urllib.request
    import webbee.modals as m
    p = tmp_path / "v.wav"
    p.write_bytes(b"x" * 2000)
    monkeypatch.setattr(m, "_voice_token", lambda force=False: "t")
    monkeypatch.setattr(m.shutil, "which", lambda name: None)

    def _open(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", _open)
    assert m.transcribe_audio_file(str(p), api_url="https://x") == ""
    assert m.LAST_VOICE_WHY == "voice is not enabled for your account"
