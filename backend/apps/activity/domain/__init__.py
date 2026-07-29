"""Pure domain of the activity context: the audit command, the audit entry and the errors.

Nothing here imports Django or another app, so the shape and the invariants of an audit
record can be exercised on ``SimpleTestCase`` without a database.
"""
