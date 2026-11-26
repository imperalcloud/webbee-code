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
