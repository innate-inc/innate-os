"""Address normalization: what a human types vs what NavClient needs."""

from innate_nav.utils import server_url

FALLBACK = "http://host.docker.internal:8900"


def test_empty_keeps_the_configured_server():
    assert server_url("", FALLBACK) == FALLBACK
    assert server_url("   ", FALLBACK) == FALLBACK


def test_a_bare_address_gets_the_default_port():
    assert server_url("10.0.0.4", FALLBACK) == "http://10.0.0.4:8900"
    assert server_url("gpu-box.local", FALLBACK) == "http://gpu-box.local:8900"


def test_an_explicit_port_is_kept():
    assert server_url("10.0.0.4:9000", FALLBACK) == "http://10.0.0.4:9000"


def test_a_full_url_passes_through_untouched():
    """The scheme is load-bearing: https:// is what makes the stream wss://."""
    assert server_url("https://nav.example.com", FALLBACK) == "https://nav.example.com"
    assert server_url("http://10.0.0.4:8900/", FALLBACK) == "http://10.0.0.4:8900"


def test_ipv6_gets_the_brackets_that_make_a_port_unambiguous():
    assert server_url("::1", FALLBACK) == "http://[::1]:8900"
    assert server_url("[::1]:9000", FALLBACK) == "http://[::1]:9000"
    assert server_url("[fe80::1]", FALLBACK) == "http://[fe80::1]:8900"
