"""Database-free tests of the engine.

Every class here is a ``SimpleTestCase``, which forbids database access: the purity of ``domain/``
is proved by the base class rather than asserted in review, so a strategy that grows an ORM call
fails these tests instead of passing them.
"""
