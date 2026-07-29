"""Pure prioritization domain: signals, specifications, scoring.

Nothing in this package imports Django, another app, or reads a clock. Every fact a signal
or a specification needs arrives inside :class:`~apps.prioritization.domain.types.SignalInput`
or :class:`~apps.prioritization.domain.types.ProjectRiskInput`, including ``now``. That is what
makes the ranking reproducible: the same input always produces the same score and the same
reasons, in a test with no database and in production.
"""
