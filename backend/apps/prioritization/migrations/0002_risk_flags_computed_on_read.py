"""Drop ``prioritization_riskflag``: risk flags become a derivation, not a table (ADR 0011).

Overdue is ``due_date < today``, stale is ``last_activity < now - N days``, and blocked,
no-next-step, no-target-date and owner-overloaded are pure functions of rows that already exist.
Storing them bought nothing and cost invalidation, so the specifications are evaluated on read and
the rows go. Nothing reads this table after this migration; there is no data to migrate anywhere,
because every row in it was reproducible from the rows that remain.

Irreversible in practice rather than in principle: the schema would come back from
``0001_initial``, and the flags would be re-derived on the next recomputation.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("prioritization", "0001_initial")]

    operations = [
        migrations.RemoveIndex(
            model_name="riskflag",
            name="prioritization_open_flags",
        ),
        migrations.RemoveIndex(
            model_name="riskflag",
            name="prioritization_flag_severity",
        ),
        migrations.RemoveIndex(
            model_name="riskflag",
            name="prioritization_flag_detected",
        ),
        migrations.RemoveConstraint(
            model_name="riskflag",
            name="prioritization_one_open_flag_per_code",
        ),
        migrations.RemoveConstraint(
            model_name="riskflag",
            name="prioritization_flag_cleared_after_detected",
        ),
        migrations.DeleteModel(
            name="RiskFlag",
        ),
    ]
