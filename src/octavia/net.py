"""What every integration that goes online shares: which certificates to trust, and how to introduce herself."""

from __future__ import annotations

import functools
import logging
import ssl
from importlib import metadata

log = logging.getLogger(__name__)


@functools.cache
def wikimedia_user_agent() -> str:
    """The User-Agent for Wikimedia's sites (Commons, Wikipedia), in the form their policy asks for.

    Wikimedia answers HTTP 403 to clients whose User-Agent doesn't follow its policy
    (https://meta.wikimedia.org/wiki/User-Agent_policy): "name/version (a link or address to learn about it)
    library/version". A bare "Octavia/1.0 (description)" without a link was refused, so both sound tools failed.
    """
    import httpx

    try:
        version = metadata.version("octavia")
    except metadata.PackageNotFoundError:
        version = "0.1"
    return (
        f"Octavia/{version} (personal Windows voice assistant; https://meta.wikimedia.org/wiki/User-Agent_policy) "
        f"httpx/{httpx.__version__}"
    )


@functools.cache
def tls_context() -> ssl.SSLContext:
    """Trust what the browser trusts: Windows' certificate store, plus httpx's usual certifi bundle.

    httpx on its own only trusts certifi, so where antivirus HTTPS scanning, a school or work network re-signs
    connections with its own certificate (installed in Windows, so the browser is fine) every request failed. Pass it
    as ``verify=`` to an httpx client (or ``ssl=`` to a websocket).
    """
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError) as exc:
        log.debug("Using only the Windows certificate store: %s", exc)
    return context
