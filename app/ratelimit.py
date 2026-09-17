"""Per-IP rate limiting for the ``/data`` and ``/search`` endpoints.

A cheap guard against a bot hammering the API and running up Container Apps
compute/egress cost. The limiter lives in its own module so both ``app.main``
(which registers the exception handler) and ``app.indicators`` (which decorates
the route) can import it without an import cycle.

Limits are passed as callables (``lambda: settings.data_rate_limit``) so they
are read per request rather than frozen at import, which lets tests lower them.

The in-memory store is per-replica; with a small ``max_replicas`` the effective
limit scales with the replica count, which is acceptable for a cost guard. A
shared store (Redis) would be the next step if that ever matters.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.settings import settings


def _client_ip(request: Request) -> str:
    """Real client IP. Azure Container Apps' ingress overwrites ``X-Forwarded-For``
    with the edge client address, so its first hop is trustworthy here."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip, enabled=settings.rate_limit_enabled)
