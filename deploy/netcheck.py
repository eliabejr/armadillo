"""Network settings and checks for deploy/install.sh. Standard library only; run with `python -I`.

  netcheck.py filter-config
      Prints the unit's network settings, one per line, with warnings on stderr:
        resolv_conf=<file to bind over /etc/resolv.conf, or empty>
        allow=<DNS resolvers on private networks that IPAddressAllow must open>
        deny=<IPAddressDeny: loopback, private ranges and this machine's own prefixes>

  netcheck.py probe CGROUP_DIR      (as root)
      Proves that the kernel filter of the service in CGROUP_DIR drops connections
      to loopback (127.0.0.1, 127.0.0.53, ::1) and to this machine's own addresses.
      Exit 0: all dropped; 1: something was reachable; 2: could not join the cgroup;
      3: inconclusive (the control connection from outside the cgroup failed).

  netcheck.py watch UNIT            (as root, from a Type=notify unit)
      Keeps this machine's prefixes in UNIT's IPAddressDeny while addresses change
      (a VM that gets a new IPv6 when it resumes, SLAAC, DHCP): checks once, reports
      ready, then checks again after every address change. Exits 1 if it cannot
      deny a prefix, so that a unit bound to it stops.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import select
import socket
import subprocess
import sys
import time

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

RESOLV_CONF = "/etc/resolv.conf"
# systemd-resolved's list of upstream servers, for hosts whose resolv.conf only
# names its stub (127.0.0.53).
RESOLVED_UPLINK = "/run/systemd/resolve/resolv.conf"
# Allowing 127.0.0.53 would open every port on it: Linux delivers that address to
# services bound to 0.0.0.0 (sshd, Docker published ports).
DENY_KEYWORDS = ("localhost", "link-local", "multicast")
DENY_NETWORKS = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
# What the keywords stand for in systemd.resource-control(5).
_KEYWORD_NETWORKS = ("127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/64", "224.0.0.0/4", "ff00::/8")
_DENIED = tuple(ipaddress.ip_network(n) for n in DENY_NETWORKS + _KEYWORD_NETWORKS)
PROBE_TIMEOUT_S = 2.0
# An IPv6 address says little about its neighbours (a VM may get a single /128), so
# the whole /64 around it is denied: its subnet and the temporary addresses SLAAC
# rotates through.
IPV6_PREFIX = 64
# Address events, and IPv4 route events for AnyIP ranges (`ip route add local ...`).
RTMGRP_IPV4_ROUTE = 0x40
RTMGRP_IPV4_IFADDR = 0x10
RTMGRP_IPV6_IFADDR = 0x100
SETTLE_S = 0.2
SETTLE_MAX_S = 0.5


def _unwrap(ip: Address) -> Address:
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def nameservers(text: str) -> list[Address]:
    found: list[Address] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            try:
                ip = _unwrap(ipaddress.ip_address(fields[1].split("%")[0]))
            except ValueError:
                continue
            if ip not in found:
                found.append(ip)
    return found


def _fib_local(fib_trie: str):
    """(address, prefix length) of each range /proc/net/fib_trie routes to this machine."""
    last = ""
    for line in fib_trie.splitlines():
        fields = line.split()
        if fields[:1] == ["|--"] and len(fields) == 2:  # a leaf: one address, followed by its routes
            last = fields[1]
        elif len(fields) == 3 and fields[0][:1] == "/" and fields[1:] == ["host", "LOCAL"]:
            yield last, fields[0][1:]


def _inet6(if_inet6: str):
    """(address, prefix length) of each line of /proc/net/if_inet6."""
    for line in if_inet6.splitlines():
        fields = line.split()
        if len(fields) >= 3 and len(fields[0]) == 32:
            try:
                ip = ipaddress.IPv6Address(":".join(fields[0][i : i + 4] for i in range(0, 32, 4)))
                yield ip, int(fields[2], 16)
            except ValueError:
                continue


def own_addresses(fib_trie: str, if_inet6: str) -> list[Address]:
    """This machine's addresses, from /proc/net/fib_trie and /proc/net/if_inet6."""
    found: list[Address] = []
    for text, plen in _fib_local(fib_trie):
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        if plen == "32" and ip not in found:
            found.append(ip)
    for ip, _ in _inet6(if_inet6):
        if ip not in found:
            found.append(ip)
    return found


def own_prefixes(fib_trie: str, if_inet6: str) -> list[Network]:
    """What to deny around this machine's addresses: each IPv4 range routed to the
    machine itself (its addresses, and AnyIP ranges such as 127.0.0.0/8), and the
    /64 around each IPv6 address (or its on-link prefix, if that is shorter)."""
    found: list[Network] = []

    def add(net: Network) -> None:
        if net not in found:
            found.append(net)

    for text, plen in _fib_local(fib_trie):
        try:
            add(ipaddress.IPv4Network(f"{text}/{plen}", strict=False))
        except ValueError:
            continue
    for ip, plen in _inet6(if_inet6):
        if not (ip.is_loopback or ip.is_link_local):
            plen = min(plen, IPV6_PREFIX)
        add(ipaddress.IPv6Network((ip, plen), strict=False))
    return found


def local_addresses() -> list[Address]:
    return own_addresses(_read("/proc/net/fib_trie"), _read("/proc/net/if_inet6"))


def _covered(net: Network, nets: list[Network]) -> bool:
    return any(net.version == n.version and net.subnet_of(n) for n in nets)


def _fmt(net: Network) -> str:
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


def _denied(ip: Address) -> bool:
    return any(ip.version == net.version and ip in net for net in _DENIED)


def filter_config(
    resolv_conf: str, uplink: str | None, own: list[Network]
) -> tuple[str, list[str], list[str], list[str]]:
    """Return (file to bind over /etc/resolv.conf or "", allow, deny, warnings).

    `uplink` is the text of systemd-resolved's upstream list, or None if it has none.
    `own` is this machine's prefixes (own_prefixes).
    """
    bind = ""
    servers = nameservers(resolv_conf)
    if servers and all(ip.is_loopback for ip in servers) and uplink is not None and nameservers(uplink):
        bind, servers = RESOLVED_UPLINK, nameservers(uplink)
    allow: list[str] = []
    warnings: list[str] = []
    usable = 0
    for ip in servers:
        if ip.is_loopback:
            reason = "on loopback"
        elif ip.is_link_local:
            reason = "link-local, where cloud metadata servers live"
        elif ip.is_unspecified or ip.is_multicast:
            reason = "not a unicast address"
        elif any(ip.version == net.version and ip in net for net in own):
            reason = "one of this machine's own addresses, or next to one"
        else:
            usable += 1
            if _denied(ip):
                allow.append(str(ip))
            continue
        warnings.append(f"not allowing DNS resolver {ip}: it is {reason}, and allowing it opens all of its ports")
    if not usable:
        warnings.append("the service has no DNS resolver it may use; list a public or private-network one in /etc/resolv.conf")
    extra = [_fmt(net) for net in own if not _covered(net, list(_DENIED))]
    deny = [*DENY_KEYWORDS, *DENY_NETWORKS, *extra]
    return bind, allow, deny, warnings


def _connect_all(targets: list[Address], port: int, timeout: float) -> dict[Address, str]:
    """Open a TCP connection to each target at once; result per target: connected, dropped or an errno name."""
    results: dict[Address, str] = {}
    pending: dict[socket.socket, Address] = {}
    for ip in targets:
        try:
            sock = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:  # no IPv6 on this machine
            results[ip] = errno.errorcode.get(exc.errno or 0, str(exc))
            continue
        sock.setblocking(False)
        code = sock.connect_ex((str(ip), port))
        if code in (errno.EINPROGRESS, errno.EWOULDBLOCK):
            pending[sock] = ip
            continue
        results[ip] = "connected" if code == 0 else errno.errorcode.get(code, str(code))
        sock.close()
    deadline = time.monotonic() + timeout
    while pending and (left := deadline - time.monotonic()) > 0:
        _, ready, _ = select.select([], list(pending), [], left)
        for sock in ready:
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            results[pending.pop(sock)] = "connected" if code == 0 else errno.errorcode.get(code, str(code))
            sock.close()
    for sock, ip in pending.items():
        results[ip] = "dropped"
        sock.close()
    return results


def probe(cgroup_dir: str, timeout: float = PROBE_TIMEOUT_S) -> int:
    targets: list[Address] = [ipaddress.ip_address(a) for a in ("127.0.0.1", "127.0.0.53", "::1")]
    targets += [ip for ip in local_addresses() if not ip.is_loopback and not ip.is_link_local]
    # A listener on all addresses, opened outside the service's cgroup.
    if socket.has_dualstack_ipv6():
        server = socket.create_server(("::", 0), family=socket.AF_INET6, dualstack_ipv6=True)
    else:
        server = socket.create_server(("0.0.0.0", 0))
    port = server.getsockname()[1]
    with server:
        control = _connect_all(targets, port, timeout)
        if control.get(targets[0]) != "connected":
            print(f"inconclusive: the control connection to 127.0.0.1 failed ({control.get(targets[0])})")
            return 3
        checked = [ip for ip in targets if control[ip] == "connected"]
        for ip in targets:
            if ip not in checked:
                print(f"{ip}: skipped, not reachable from outside the cgroup either ({control[ip]})")
        try:
            with open(os.path.join(cgroup_dir, "cgroup.procs"), "w") as f:
                f.write(str(os.getpid()))
        except OSError as exc:
            print(f"could not join {cgroup_dir}: {exc}")
            return 2
        inside = _connect_all(checked, port, timeout)
    # The filter drops the packets, so a blocked connection times out. EPERM would
    # mean the same; anything else (a refusal, a reset) means packets got through.
    failed = False
    for ip in checked:
        blocked = inside[ip] in ("dropped", "EPERM", "EACCES")
        failed = failed or not blocked
        print(f"{ip}: {'blocked' if blocked else 'REACHABLE (' + inside[ip] + ')'}")
    return 1 if failed else 0


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def applied_deny(unit: str) -> list[Network] | None:
    """The unit's IPAddressDeny as systemd applies it; None if it cannot be read."""
    shown = _systemctl("show", "-p", "IPAddressDeny", "--value", unit)
    if shown.returncode:
        return None
    try:
        nets = [ipaddress.ip_network(entry, strict=False) for entry in shown.stdout.split()]
    except ValueError:
        return None
    return nets or None  # an empty list means an unknown unit, or one without the filter


def sync(unit: str) -> bool:
    """Add to the unit's IPAddressDeny each of this machine's prefixes it lacks.

    True if every prefix is denied. The change applies to the running service at once;
    it lasts until the next boot (a runtime drop-in), when the watcher checks again.
    """
    fib_trie = _read("/proc/net/fib_trie")
    if not fib_trie:
        print("cannot read /proc/net/fib_trie", flush=True)
        return False
    applied = applied_deny(unit)
    if applied is None:
        print(f"cannot read the IPAddressDeny of {unit}", flush=True)
        return False
    missing = [net for net in own_prefixes(fib_trie, _read("/proc/net/if_inet6")) if not _covered(net, applied)]
    if not missing:
        return True
    listed = " ".join(_fmt(net) for net in missing)
    print(f"this machine has new addresses; denying {listed} to {unit}", flush=True)
    done = _systemctl("set-property", "--runtime", unit, f"IPAddressDeny={listed}")
    applied = applied_deny(unit)
    if done.returncode or applied is None or not all(_covered(net, applied) for net in missing):
        print(f"could not deny {listed} to {unit}: {done.stderr.strip()}", flush=True)
        return False
    return True


def _notify(state: str) -> None:
    """Report to systemd, for a Type=notify unit."""
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(path)
        sock.sendall(state.encode())


def _next_event(events: socket.socket) -> bool:
    try:
        return bool(events.recv(65536))
    except OSError as exc:  # ENOBUFS: the kernel dropped events; look at the addresses anyway
        if exc.errno == errno.ENOBUFS:
            return True
        raise


def watch(unit: str, events: socket.socket | None = None) -> int:
    if events is None:
        events = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        events.bind((0, RTMGRP_IPV4_IFADDR | RTMGRP_IPV6_IFADDR | RTMGRP_IPV4_ROUTE))
    # Subscribed before the first check, so a change made during it is not missed.
    with events:
        if not sync(unit):
            return 1
        _notify("READY=1")
        while _next_event(events):
            # Changes come in bursts: let them settle, then look once, but never
            # later than SETTLE_MAX_S after the first event, however long it lasts.
            deadline = time.monotonic() + SETTLE_MAX_S
            while (left := deadline - time.monotonic()) > 0 and select.select([events], [], [], min(SETTLE_S, left))[0]:
                if not _next_event(events):
                    break
            if not sync(unit):
                return 1
    print("the address events stopped", flush=True)
    return 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["filter-config"] and len(argv) == 1:
        uplink = _read(RESOLVED_UPLINK) if os.path.exists(RESOLVED_UPLINK) else None
        fib_trie = _read("/proc/net/fib_trie")
        own = own_prefixes(fib_trie, _read("/proc/net/if_inet6"))
        bind, allow, deny, warnings = filter_config(_read(RESOLV_CONF), uplink, own)
        if not fib_trie:
            warnings.append("cannot read /proc/net/fib_trie: this machine's IPv4 addresses are not denied")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(f"resolv_conf={bind}\nallow={' '.join(allow)}\ndeny={' '.join(deny)}")
        return 0
    if argv[:1] == ["probe"] and len(argv) == 2:
        return probe(argv[1])
    if argv[:1] == ["watch"] and len(argv) == 2:
        return watch(argv[1])
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
