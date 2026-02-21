"""Client library for innate-os proxy services.

This package provides :class:`ProxyClient`, a thin wrapper around
:mod:`innate_proxy` that adds a ``config`` dict (voice IDs, model names)
used by brain_client nodes.  Service adapters (Cartesia, OpenAI) are
imported from ``innate_proxy`` — no local adapter code.
"""

__version__ = "0.3.0"

