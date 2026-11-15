"""deploy/netcheck.py: the installer's DNS and IP-filter settings, and its filter probe."""

from __future__ import annotations

import errno
import importlib.util
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("netcheck", Path(__file__).parents[2] / "deploy" / "netcheck.py")
netcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(netcheck)

ip = ipaddress.ip_address
net = ipaddress.ip_network
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
        ("2001:db8::53", "next to one"),
    ],
)
def test_resolvers_that_would_open_this_machine_are_not_allowed(server, reason):
    own = [net("192.168.1.10/32"), net("2001:db8::/64")]
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
def test_own_public_prefixes_join_the_deny_list():
    assert netcheck.own_addresses(FIB_TRIE, IF_INET6) == [
        ip("127.0.0.1"), ip("172.16.0.2"), ip("203.0.113.5"),
        ip("::1"), ip("fe80::a8bb:ccff:fe00:1122"), ip("2001:db8::5")]
    own = netcheck.own_prefixes(FIB_TRIE, IF_INET6)
    assert own == [net("127.0.0.0/8"), net("127.0.0.1/32"), net("172.16.0.2/32"), net("203.0.113.5/32"),
                   net("::1/128"), net("fe80::/64"), net("2001:db8::/64")]
    _, _, deny, _ = netcheck.filter_config("nameserver 1.1.1.1\n", None, own)
    assert deny[: len(netcheck.DENY_KEYWORDS) + len(netcheck.DENY_NETWORKS)] == [
        *netcheck.DENY_KEYWORDS, *netcheck.DENY_NETWORKS]
    # Already covered by a range: loopback, 172.16/12, link-local. 2001:db8:: is not.
    assert deny[len(netcheck.DENY_KEYWORDS) + len(netcheck.DENY_NETWORKS):] == ["203.0.113.5", "2001:db8::/64"]


@pytest.mark.case("NET-06")
def test_own_address_as_resolver_is_not_usable_even_if_public():
    _, allow, deny, warnings = netcheck.filter_config("nameserver 203.0.113.5\n", None, [net("203.0.113.5/32")])
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


@pytest.mark.case("NET-11")
def test_prefixes_widen_ipv6_to_its_64_and_keep_anyip_ranges():
    fib_trie = "Local:\n     |-- 198.51.100.0\n        /24 host LOCAL\n     |-- 198.51.100.255\n        /32 link BROADCAST\n"
    if_inet6 = (
        "20010db8000a7b1dc0730000000000d6 03 80 00 82     eth0\n"  # a single /128, as on a cloud VM
        "20010db8000100000000000000000001 02 30 00 80     eth1\n"  # a /48 on-link: kept whole
        "00000000000000000000000000000001 01 80 10 80       lo\n"
    )
    assert netcheck.own_prefixes(fib_trie, if_inet6) == [
        net("198.51.100.0/24"), net("2001:db8:a:7b1d::/64"), net("2001:db8:1::/48"), net("::1/128")]
    assert netcheck.own_addresses(fib_trie, if_inet6)[0] == ip("2001:db8:a:7b1d:c073::d6")


# --- the watcher -----------------------------------------------------------------

BASE_DENY = "127.0.0.0/8 ::1/128 fe80::/64 172.16.0.0/12 2001:db8:a:7b37:1afd::ba/128"


@pytest.fixture
def host(monkeypatch):
    """A machine whose IPv6 has just changed, and a fake systemctl."""
    state = {"deny": BASE_DENY, "calls": [], "apply": True}
    files = {
        "/proc/net/fib_trie": "Local:\n     |-- 172.16.0.2\n        /32 host LOCAL\n",
        "/proc/net/if_inet6": "20010db8000a7b1dc0730000000000d6 03 80 00 82     eth0\n",
    }
    monkeypatch.setattr(netcheck, "_read", lambda path: files.get(path, ""))

    def systemctl(*args):
        state["calls"].append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, state["deny"] + "\n", "")
        if state["apply"]:
            state["deny"] += " " + args[-1].removeprefix("IPAddressDeny=")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(netcheck, "_systemctl", systemctl)
    state["files"] = files
    return state


@pytest.mark.case("NET-12")
def test_sync_adds_only_the_missing_prefix_and_proves_it(host):
    assert netcheck.sync("scrape-api.service") is True
    sets = [c for c in host["calls"] if c[0] == "set-property"]
    assert sets == [("set-property", "--runtime", "scrape-api.service", "IPAddressDeny=2001:db8:a:7b1d::/64")]
    host["calls"].clear()
    assert netcheck.sync("scrape-api.service") is True  # nothing left to add
    assert [c[0] for c in host["calls"]] == ["show"]


@pytest.mark.case("NET-13")
def test_sync_fails_closed(host):
    host["apply"] = False  # systemd took the call, but the list did not change
    assert netcheck.sync("scrape-api.service") is False

    def set_calls():
        return [c for c in host["calls"] if c[0] == "set-property"]

    host["calls"].clear()
    host["deny"] = ""  # an unknown unit: show prints an empty list
    assert netcheck.sync("scrape-api.service") is False
    assert set_calls() == []
    host["deny"], host["files"]["/proc/net/fib_trie"] = BASE_DENY, ""
    assert netcheck.sync("scrape-api.service") is False
    assert set_calls() == []


@pytest.mark.case("NET-14")
@pytest.mark.skipif(sys.platform != "linux", reason="abstract unix sockets")
def test_watch_reports_ready_and_checks_once_per_burst(monkeypatch):
    calls = []
    monkeypatch.setattr(netcheck, "sync", lambda unit: calls.append(unit) or True)
    monkeypatch.setattr(netcheck, "SETTLE_S", 0.05)
    name = f"armadillo-test-{os.getpid()}"
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
    events, kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify, kernel:
        notify.bind("\0" + name)
        for _ in range(3):  # one burst of address messages
            kernel.send(b"RTM_NEWADDR")
        # Then the event source goes away: the watcher must exit, never idle.
        timer = threading.Timer(0.5, kernel.close)
        timer.start()
        assert netcheck.watch("scrape-api.service", events) == 1
        timer.join()
        assert notify.recv(64) == b"READY=1"
    assert calls == ["scrape-api.service"] * 2  # the first check, then one for the burst


@pytest.mark.case("NET-15")
def test_watch_exits_when_a_check_fails(monkeypatch):
    monkeypatch.setattr(netcheck, "sync", lambda unit: False)
    monkeypatch.setattr(netcheck, "_notify", lambda state: pytest.fail("reported ready"))
    events, kernel = socket.socketpair()
    with kernel:
        assert netcheck.watch("scrape-api.service", events) == 1
    # A check that fails after the watcher reported ready ends it too.
    results, ready = iter([True, False]), []
    monkeypatch.setattr(netcheck, "sync", lambda unit: next(results))
    monkeypatch.setattr(netcheck, "_notify", ready.append)
    events, kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with kernel:
        kernel.send(b"RTM_NEWADDR")
        assert netcheck.watch("scrape-api.service", events) == 1
    assert ready == ["READY=1"]


@pytest.mark.case("NET-16")
def test_lost_events_still_count_as_a_change():
    class Events:
        def __init__(self, outcome):
            self.outcome = outcome

        def recv(self, size):
            if isinstance(self.outcome, OSError):
                raise self.outcome
            return self.outcome

    assert netcheck._next_event(Events(OSError(errno.ENOBUFS, "lost"))) is True
    assert netcheck._next_event(Events(b"RTM_NEWADDR")) is True
    assert netcheck._next_event(Events(b"")) is False
    with pytest.raises(OSError):
        netcheck._next_event(Events(OSError(errno.EBADF, "closed")))


@pytest.mark.case("NET-17")
def test_a_stream_of_events_does_not_delay_the_check(monkeypatch):
    monkeypatch.setattr(netcheck, "SETTLE_S", 0.1)
    monkeypatch.setattr(netcheck, "SETTLE_MAX_S", 0.3)
    monkeypatch.setattr(netcheck, "_notify", lambda state: None)
    checks = []
    monkeypatch.setattr(netcheck, "sync", lambda unit: checks.append(time.monotonic()) or len(checks) == 1)
    events, kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    stop = threading.Event()

    def flap():  # an event every 20 ms, shorter than SETTLE_S each time, for up to 3 s
        end = time.monotonic() + 3
        while not stop.wait(0.02) and time.monotonic() < end:
            kernel.send(b"RTM_NEWADDR")

    sender = threading.Thread(target=flap)
    with kernel:
        sender.start()
        try:
            assert netcheck.watch("scrape-api.service", events) == 1
        finally:
            stop.set()
            sender.join()
    assert len(checks) == 2 and checks[1] - checks[0] < 1.0
