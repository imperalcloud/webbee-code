"""Live 2026-11-14 (Windows, Python 3.11): a server-reset socket (WinError 10054 in the Proactor's connection-lost
callback) was painted over the dock by prompt_toolkit's loop handler and stopped on "Press ENTER to continue..."."""
import asyncio
import inspect
import logging

from webbee import loop_errors


def test_a_reset_connection_is_dropped_silently(caplog):
    exc = ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
    with caplog.at_level(logging.WARNING, logger="webbee.loop"):
        loop_errors.handle_loop_exception(None, {"message": "Exception in callback", "exception": exc})
        loop_errors.handle_loop_exception(None, {"message": "x", "exception": ConnectionAbortedError()})
        loop_errors.handle_loop_exception(None, {"message": "x", "exception": BrokenPipeError()})
    assert caplog.records == []


def test_any_other_loop_exception_goes_to_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger="webbee.loop"):
        loop_errors.handle_loop_exception(None, {"message": "Task exception was never retrieved",
                                                 "exception": ValueError("boom")})
        loop_errors.handle_loop_exception(None, {"message": "no exception here", "handle": "<Handle x>"})
    text = caplog.text
    assert "ValueError: boom" in text and "no exception here" in text


def test_install_puts_the_handler_on_the_running_loop():
    async def go():
        loop_errors.install()
        return asyncio.get_running_loop().get_exception_handler()
    assert asyncio.run(go()) is loop_errors.handle_loop_exception


def test_a_reset_inside_a_callback_never_reaches_the_default_handler(capsys):
    async def go():
        loop_errors.install()
        loop = asyncio.get_running_loop()

        def connection_lost():
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        loop.call_soon(connection_lost)
        await asyncio.sleep(0.01)
    asyncio.run(go())
    assert "10054" not in capsys.readouterr().err


def test_the_dock_runs_without_prompt_toolkits_own_handler():
    from webbee import tui
    src = inspect.getsource(tui)
    assert "app.run_async(set_exception_handler=False)" in src and "loop_errors.install()" in src
