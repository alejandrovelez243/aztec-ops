"""Pure domain layer of the ``work`` context.

Nothing in this package imports Django or another app: it takes and returns Pydantic
models and primitives only. That is what lets the dependency-cycle rule, the blocker
vocabulary and the command shapes be tested with ``SimpleTestCase``, which forbids
database access, instead of being trusted.
"""
