"""HTTP surface of the audit trail: one read, and no write.

There is no ``POST``, no ``PATCH`` and no ``DELETE`` here, and that is the contract rather than an
omission. Records are appended by the services that cause them, so an HTTP write would be a second
author of the trail — and a trail with two authors is one that can disagree with the changes it
claims to describe.
"""

from apps.activity.api.routers import router

__all__ = ["router"]
