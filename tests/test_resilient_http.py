from webbee.http import WakeDetector, make_client, socket_options
from webbee.config import Config


def test_a_long_idle_wait_is_not_a_sleep_but_a_clock_drift_is():
    t = {"wall": 1000.0, "mono": 50.0}
    d = WakeDetector(8.0, wall=lambda: t["wall"], mono=lambda: t["mono"])
    t["wall"] += 60; t["mono"] += 60          # 60s idle: both clocks move
    assert d.check() is False
    t["wall"] += 600; t["mono"] += 2          # the laptop slept ~10 minutes
    assert d.check() is True
    t["wall"] += 4; t["mono"] += 4
    assert d.check() is False


def test_the_client_sockets_carry_keepalive():
    import socket
    opts = socket_options()
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in opts
    client = make_client(Config(api_url="https://api.imperal.io", panel_url="https://panel.imperal.io"))
    assert client.timeout.read == 10.0
