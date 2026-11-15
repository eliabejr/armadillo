"""deploy/netcheck.py: the installer's DNS and IP-filter settings, and its filter probe."""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import socket
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("netcheck", Path(__file__).parents[2] / "deploy" / "netcheck.py")
netcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(netcheck)

ip = ipaddress.ip_address
STUB = "# managed by systemd-resolved\nnameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n"
FIB_TRIE = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        +-- 127.0.0.0/31 1 0 0
           |-- 127.0.0.0
              /8 host LOCAL
           |-- 127.0.0.1
              /32 host LOCAL
        |-- 127.255.255.255
           /32 link BROADCAST
     +-- 172.16.0.0/30 2 0 2
        |-- 172.16.0.2
           /32 host LOCAL
        |-- 172.16.0.3
           /32 link BROADCAST
     |-- 203.0.113.5
        /32 host LOCAL
Local:
     |-- 172.16.0.2
        /32 host LOCAL
"""
IF_INET6 = """00000000000000000000000000000001 01 80 10 80       lo
fe80000000000000a8bbccfffe001122 02 40 20 80     eth0
20010db8000000000000000000000005 02 40 00 80     eth0
"""


@pytest.mark.case("NET-01")
def test_resolved_stub_is_replaced_by_its_upstream_servers_never_allowed():
    bind, allow, deny, warnings = netcheck.filter_config(STUB, "nameserver 10.0.0.2\nnameserver 1.1.1.1\n", [])
    assert bind == "/run/systemd/resolve/resolv.conf"
    assert allow == ["10.0.0.2"]
    assert "127.0.0.53" not in allow and "localhost" in deny
    assert warnings == []


@pytest.mark.case("NET-02")
def test_public_resolvers_need_nothing():
    bind, allow, _, warnings = netcheck.filter_config("nameserver 1.1.1.1\nnameserver 8.8.8.8\n", None, [])
    assert (bind, allow, warnings) == ("", [], [])


@pytest.mark.case("NET-03")
@pytest.mark.parametrize(
    "server, reason",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        ("169.254.169.254", "link-local"),
        ("fe80::1%eth0", "link-local"),
        ("0.0.0.0", "unicast"),
        ("192.168.1.10", "own addresses"),
    ],
)
def test_resolvers_that_would_open_this_machine_are_not_allowed(server, reason):
    own = [ip("192.168.1.10")]
    _, allow, _, warnings = netcheck.filter_config(f"nameserver {server}\n", None, own)
    assert allow == []
    assert reason in warnings[0]
    assert "no DNS resolver it may use" in warnings[-1]


@pytest.mark.case("NET-04")
def test_stub_without_upstream_servers_warns_instead_of_allowing_it():
    bind, allow, _, warnings = netcheck.filter_config(STUB, "# no servers\n", [])
    assert (bind, allow) == ("", [])
    assert any("127.0.0.53" in w for w in warnings)
    assert netcheck.filter_config(STUB, None, [])[:2] == ("", [])


@pytest.mark.case("NET-05")
def test_own_public_addresses_join_the_deny_list():
    own = netcheck.own_addresses(FIB_TRIE, IF_INET6)
    assert own == [ip("127.0.0.1"), ip("172.16.0.2"), ip("203.0.113.5"),
                   ip("::1"), ip("fe80::a8bb:ccff:fe00:1122"), ip("2001:db8::5")]
    _, _, deny, _ = netcheck.filter_config("nameserver 1.1.1.1\n", None, own)
    assert deny[: len(netcheck.DENY_KEYWORDS) + len(netcheck.DENY_NETWORKS)] == [
        *netcheck.DENY_KEYWORDS, *netcheck.DENY_NETWORKS]
    # Already covered by a range: loopback, 172.16/12, link-local. 2001:db8:: is not.
    assert deny[len(netcheck.DENY_KEYWORDS) + len(netcheck.DENY_NETWORKS):] == ["203.0.113.5", "2001:db8::5"]


@pytest.mark.case("NET-06")
def test_own_address_as_resolver_is_not_usable_even_if_public():
    _, allow, deny, warnings = netcheck.filter_config("nameserver 203.0.113.5\n", None, [ip("203.0.113.5")])
    assert allow == [] and "203.0.113.5" in deny
    assert "own addresses" in warnings[0]


# --- the probe -------------------------------------------------------------------


@pytest.fixture
def no_local_addresses(monkeypatch):
    monkeypatch.setattr(netcheck, "local_addresses", lambda: [])


@pytest.fixture
def fake_cgroup(tmp_path):
    (tmp_path / "cgroup.procs").write_text("")
    return tmp_path


@pytest.mark.case("NET-07")
def test_connect_all_tells_connected_from_refused():
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        assert netcheck._connect_all([ip("127.0.0.1")], port, 1.0) == {ip("127.0.0.1"): "connected"}
    assert netcheck._connect_all([ip("127.0.0.1")], port, 1.0) == {ip("127.0.0.1"): "ECONNREFUSED"}


@pytest.mark.case("NET-08")
def test_probe_fails_when_nothing_filters(no_local_addresses, fake_cgroup, capsys):
    # The fake cgroup moves nothing, so the "inside" connections get through.
    assert netcheck.probe(str(fake_cgroup), timeout=1.0) == 1
    out = capsys.readouterr().out
    assert "127.0.0.1: REACHABLE" in out and "127.0.0.53: REACHABLE" in out
    assert (fake_cgroup / "cgroup.procs").read_text() == str(os.getpid())


@pytest.mark.case("NET-09")
def test_probe_passes_only_when_every_target_is_dropped(no_local_addresses, fake_cgroup, monkeypatch):
    real = netcheck._connect_all
    calls = []

    def inside(outcome):
        def fake(targets, port, timeout):
            calls.append(targets)
            if len(calls) == 1:
                return real(targets, port, timeout)  # the control, outside the cgroup
            return {t: outcome(t) for t in targets}
        return fake

    monkeypatch.setattr(netcheck, "_connect_all", inside(lambda t: "dropped"))
    assert netcheck.probe(str(fake_cgroup)) == 0
    assert ip("127.0.0.53") in calls[1]
    calls.clear()
    # A refusal means the packet reached the stack: not proof of a filter.
    monkeypatch.setattr(netcheck, "_connect_all", inside(lambda t: "ECONNREFUSED" if t.version == 4 else "dropped"))
    assert netcheck.probe(str(fake_cgroup)) == 1


@pytest.mark.case("NET-10")
def test_probe_cannot_join_or_has_no_control(no_local_addresses, tmp_path, monkeypatch):
    assert netcheck.probe(str(tmp_path / "missing")) == 2
    monkeypatch.setattr(netcheck, "_connect_all", lambda targets, port, timeout: {t: "dropped" for t in targets})
    assert netcheck.probe(str(tmp_path)) == 3
