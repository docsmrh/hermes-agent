"""Dual-stack loopback discovery + port-squatter handling for /browser connect.

Regression context: on Windows, an IDE debugger (VS Code js-debug) holding
127.0.0.1:9222 pushes a Chrome launched with --remote-debugging-port=9222
onto [::1]:9222 only. The old IPv4-only probe missed the live browser AND
hung against the squatter (accepts TCP, never answers HTTP), driving the
whole connect past the desktop GUI's RPC timeout
("error: request timed out: browser.manage").
"""

from __future__ import annotations

import errno
import socket
import threading

import pytest

from hermes_cli.browser_connect import (
    DEFAULT_BROWSER_CDP_PORT,
    discover_local_cdp_url,
    find_free_debug_port,
    local_port_in_use,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def ipv4_squatter():
    """A listener on the IPv4 loopback that accepts TCP but never speaks HTTP."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]

    conns: list[socket.socket] = []
    stop = threading.Event()

    def _accept_loop() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conns.append(conn)  # hold open, say nothing — like a debug adapter
            except OSError:
                continue

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        server.close()


class TestDiscoverLocalCdpUrl:
    def test_returns_none_when_nothing_listens(self):
        port = _free_port()
        assert discover_local_cdp_url(port, timeout=0.3) is None


    def test_finds_ipv6_only_endpoint(self, monkeypatch):
        """When only [::1] speaks CDP (IPv4 side squatted), discovery
        returns the IPv6 URL instead of giving up."""
        import hermes_cli.browser_connect as bc

        def _ready(url: str, timeout: float = 1.0) -> bool:
            return "[::1]" in url

        monkeypatch.setattr(bc, "is_browser_debug_ready", _ready)
        assert bc.discover_local_cdp_url(9222) == "http://[::1]:9222"


class TestLocalPortInUse:
    def test_free_port_reports_unused(self):
        assert local_port_in_use(_free_port(), timeout=0.3) is False


class TestFindFreeDebugPort:
    def test_returns_port_above_preferred(self):
        port = find_free_debug_port(DEFAULT_BROWSER_CDP_PORT)
        assert port > DEFAULT_BROWSER_CDP_PORT

    def test_skips_occupied_successor(self):
        """When preferred+1 is held on IPv4, the next candidate is chosen."""
        preferred = _free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", preferred + 1))
            blocker.listen(1)
            port = find_free_debug_port(preferred)
            assert port != preferred + 1
            assert port > preferred
        except OSError:
            pytest.skip("successor port unavailable to bind in this environment")
        finally:
            blocker.close()


def _patch_ipv6_loopback(monkeypatch, *, on_socket=None, on_bind=None):
    """Replace the ``::1`` side of ``socket.socket`` with a scripted stand-in.

    IPv4 sockets are passed through untouched so the real IPv4 loopback
    is still probed. ``on_socket()`` runs at construction
    (raise there to simulate a host with no IPv6 stack), ``on_bind(address)``
    runs at ``bind`` (raise there to simulate no ``::1`` address, or a
    squatter). Neither fakes the host OS — it scripts one address family.
    """
    real_socket = socket.socket

    class _StubV6:
        family = socket.AF_INET6

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

        def close(self):
            pass

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            if on_bind is not None:
                on_bind(address)

    def factory(family=-1, type=-1, proto=-1, fileno=None):
        if family == socket.AF_INET6:
            if on_socket is not None:
                on_socket()
            return _StubV6()
        return real_socket(family, type, proto, fileno)

    monkeypatch.setattr(socket, "socket", factory)


def _raise_errno(code: int):
    def _raise(*_args, **_kwargs):
        raise OSError(code, "simulated")

    return _raise


class TestFindFreeDebugPortIPv6Unavailable:
    """Hosts with no usable IPv6 loopback (IPv6 disabled in a container,
    ``ipv6.disable=1``): the ``::1`` probe must be skipped, not treated as
    "occupied", or the IPv4 probe never influences the result."""

    @pytest.mark.parametrize(
        "patch_kwargs",
        [
            pytest.param({"on_socket": _raise_errno(errno.EAFNOSUPPORT)}, id="no-ipv6-stack"),
            pytest.param({"on_bind": _raise_errno(errno.EADDRNOTAVAIL)}, id="no-::1-address"),
        ],
    )
    def test_skips_ipv4_occupied_successor(self, monkeypatch, patch_kwargs):
        preferred = _free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", preferred + 1))
            blocker.listen(1)
        except OSError:
            blocker.close()
            pytest.skip("successor port unavailable to bind in this environment")
        try:
            _patch_ipv6_loopback(monkeypatch, **patch_kwargs)
            port = find_free_debug_port(preferred)
            assert port != preferred + 1
            assert port > preferred
            # The chosen port must actually be free on the loopback the host
            # has, with the squatter still holding preferred + 1.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(("127.0.0.1", port))
        finally:
            blocker.close()


class TestFindFreeDebugPortDualStackIntent:
    """The dual-stack contract is preserved: a port held on ``::1`` only is
    still rejected, because a browser launched on it would be pushed onto
    the IPv4 loopback and the squatter would win the IPv6 side."""

    def test_ipv6_only_squatter_still_rejects_candidate(self, monkeypatch):
        preferred = _free_port()
        squatted = preferred + 1

        def _bind(address):
            if address[1] == squatted:
                raise OSError(errno.EADDRINUSE, "simulated squatter")

        _patch_ipv6_loopback(monkeypatch, on_bind=_bind)
        port = find_free_debug_port(preferred)
        assert port != squatted
        assert port > preferred

    def test_falls_back_when_no_loopback_family_usable(self, monkeypatch):
        """Both families unusable is a degenerate host; keep the documented
        ``preferred + 1`` fallback instead of looping or raising."""
        preferred = _free_port()
        real_socket = socket.socket

        def factory(family=-1, type=-1, proto=-1, fileno=None):
            if family in (socket.AF_INET, socket.AF_INET6):
                raise OSError(errno.EAFNOSUPPORT, "simulated")
            return real_socket(family, type, proto, fileno)

        monkeypatch.setattr(socket, "socket", factory)
        assert find_free_debug_port(preferred) == preferred + 1
