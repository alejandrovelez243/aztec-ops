"""HTTP surface of the identity context: the three authentication routes.

``api/`` translates HTTP into a service call and a service result into a response, and does nothing
else (ARCHITECTURE §7). It **never imports ``models``**: credential verification and token minting
are use cases in ``services/``, so no view learns what a password hash is. The cookie is the one
thing these routes touch directly, because a cookie is HTTP and belongs nowhere else.
"""

from apps.accounts.api.routers import router

__all__ = ["router"]
