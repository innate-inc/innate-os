"""The shim injects the developer's key, so it must not relay for the LAN.

It has to listen on every interface -- the container reaches it through
host.docker.internal, never loopback -- and run_eval.sh starts it in the
background on any host. Before this, a LAN neighbour who found port 8099 could
make billable requests with the key and read the forwarded files/cache
endpoints. Found by an adversarial review.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import ipaddress
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import gemini_shim
import pytest


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "127.0.0.53", "::1", "172.17.0.2", "172.31.255.9", "192.168.65.3", "::ffff:172.18.0.5"]
)
def test_loopback_and_the_docker_networks_are_trusted(ip: str) -> None:
    assert gemini_shim.trusted(ip)


@pytest.mark.parametrize("ip", ["192.168.1.20", "10.0.0.5", "8.8.8.8", "172.32.0.1", "2001:db8::1", "not-an-ip", ""])
def test_the_lan_and_the_internet_are_not(ip: str) -> None:
    assert not gemini_shim.trusted(ip)


def test_extra_networks_come_from_the_environment_variable() -> None:
    networks = gemini_shim.allowed_networks("10.42.0.0/16, 192.168.1.0/24")
    assert gemini_shim.trusted("10.42.3.4", networks)
    assert gemini_shim.trusted("192.168.1.20", networks)
    assert not gemini_shim.trusted("10.43.0.1", networks)
    assert gemini_shim.allowed_networks("") == gemini_shim.TRUSTED_NETWORKS
    with pytest.raises(ValueError):
        gemini_shim.allowed_networks("not-a-network")


def test_an_untrusted_client_is_refused_before_anything_goes_upstream() -> None:
    """The real handler, with loopback removed from its trusted set so the
    test's own connection is the intruder. Upstream is the discard port: had
    the request been relayed, the answer would be a 502, not a 403."""
    saved = gemini_shim.Handler.networks, gemini_shim.UPSTREAM, gemini_shim.Handler.api_key
    server = ThreadingHTTPServer(("127.0.0.1", 0), gemini_shim.Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        gemini_shim.Handler.networks = (ipaddress.ip_network("192.0.2.0/24"),)  # TEST-NET-1: nobody
        gemini_shim.UPSTREAM = "http://127.0.0.1:9"
        gemini_shim.Handler.api_key = "test-key-not-real"
        serving.start()
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1beta/models?pageSize=1", timeout=10)
        assert refused.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        gemini_shim.Handler.networks, gemini_shim.UPSTREAM, gemini_shim.Handler.api_key = saved
