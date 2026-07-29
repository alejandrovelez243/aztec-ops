"""The one test seam every bus-touching test needs: Celery running inline.

Handler tests live in the context that owns the handler, so this cannot stay private to
``test_bus``. It is a mixin rather than a decorator because it has to restore the previous
configuration on the *instance's* cleanup: ``task_always_eager`` is process-global, and a test that
left it on would silently turn every later ``.delay()`` in the run into a synchronous call.
"""

from config.celery import app as celery_app


class EagerCeleryMixin:
    """Run every task inline and let its exceptions surface in the test.

    With this on, ``transaction.on_commit`` kicking ``events.drain_outbox`` executes the drain in
    the test process instead of reaching for a broker. That is what keeps a handler test from
    depending on Redis being up, and what makes "the event was dispatched" observable at all.
    """

    def setUp(self) -> None:
        """Turn eager execution on and register its restoration before the test body runs."""
        super().setUp()  # type: ignore[misc]
        previous = (celery_app.conf.task_always_eager, celery_app.conf.task_eager_propagates)
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True

        def restore() -> None:
            celery_app.conf.task_always_eager = previous[0]
            celery_app.conf.task_eager_propagates = previous[1]

        self.addCleanup(restore)  # type: ignore[attr-defined]
