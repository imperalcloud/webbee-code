"""Ignat 06.11 §7.6: the agent took every private key of ~/.ssh and tried each on root of 11 hosts, and rewrote
~/.ssh/config without asking. A command that reads keys, sweeps them or writes there is the user's to allow."""
import os

from webbee.tools import LocalToolExecutor, ssh_guard


def test_sweeping_reading_or_writing_ssh_is_refused():
    for cmd in ('for k in ~/.ssh/id_*; do ssh -i "$k" root@10.0.0.1 true; done',
                "cat ~/.ssh/id_ed25519", "base64 $HOME/.ssh/id_rsa",
                "echo 'Host x\n  ProxyJump j' >> ~/.ssh/config", "sed -i 's/a/b/' ~/.ssh/config",
                f"cp /tmp/k {os.path.expanduser('~')}/.ssh/authorized_keys"):
        assert ssh_guard(cmd), cmd


def test_using_one_key_or_listing_stays():
    for cmd in ("ssh root@10.0.0.1 uptime", "ssh -i ~/.ssh/deploy_key root@h uptime", "ls -la ~/.ssh",
                "ssh-keygen -lf ~/.ssh/id_ed25519.pub", "git push origin main"):
        assert not ssh_guard(cmd), cmd


def test_the_executor_refuses_and_says_ask(tmp_path):
    out = LocalToolExecutor(str(tmp_path)).run("bash", {"command": "cat ~/.ssh/id_rsa"})
    assert out["ok"] is False and "ask the user" in out["content"]
