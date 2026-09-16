"""Make HTTPS use the operating system's certificate trust store.

uv-managed (python-build-standalone) interpreters compile OpenSSL with
``OPENSSLDIR=/etc/ssl``, which does not exist on RHEL 8, so ``ssl`` loads no
CAs at all, so *every* certificate fails verification — the GitLab server's
is a perfectly ordinary Let's Encrypt one. That is the difference between
``uvx dls-d2bpm-tools`` and running from a venv built on the system Python.
``certifi`` would also fix this, but ``truststore`` defers to the OS trust
store, so it needs no bundle of its own to keep current and picks up any
internal CA a Diamond host has been given.
"""

from __future__ import annotations

__all__ = ["install_system_trust"]


def install_system_trust() -> bool:
    """Route ``ssl`` default contexts through the OS trust store.

    Returns whether the injection happened. Best effort: a missing or
    unsupported ``truststore`` leaves stock ``ssl`` behaviour in place rather
    than stopping the tool, since the filesystem source needs no HTTPS.
    """
    try:
        import truststore
    except ImportError:  # pragma: no cover - truststore is a hard dependency
        return False

    try:
        truststore.inject_into_ssl()
    except (NotImplementedError, OSError):
        return False
    return True
