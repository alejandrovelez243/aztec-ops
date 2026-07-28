"""Pure domain of the events context: the envelope, the topic catalog and the typed errors.

Nothing in this package imports Django, a model or another app. That is what lets every other
bounded context depend on the envelope without depending on the bus implementation, and what
lets the envelope be tested on ``SimpleTestCase``.
"""
