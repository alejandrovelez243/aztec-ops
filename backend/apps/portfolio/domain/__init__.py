"""Pure portfolio domain: errors and value objects, with no Django and no other app.

Nothing in this package imports ``django.db``, a model, or another bounded context. That is what
lets every rule stated here be tested on ``SimpleTestCase``, which forbids database access, so the
purity is proved by the test base class rather than by discipline (BACKEND §7).
"""
