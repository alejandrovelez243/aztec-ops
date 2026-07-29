"""Turn ``Project.currency`` from a three-letter string into a ``catalog.Currency`` reference.

Written as a five-step swap rather than a single ``AlterField`` because PostgreSQL cannot cast a
``varchar(3)`` column to a foreign-key integer: the codes already stored have to be *resolved*
against the taxonomy, which is a data step and not a type change. The new column is added
nullable, filled from the old one, and only then made NOT NULL, so an existing portfolio survives
the migration instead of losing what its contracts were billed in.

Failure mode: a project stored a code the taxonomy has no row for. The forward step raises rather
than defaulting to USD, because silently repricing a Colombian contract in dollars is the one
outcome worse than a failed migration.
"""

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def resolve_currency_codes(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Point every project at the ``Currency`` row carrying the code it already stored.

    One query per distinct code — there are two in the seed data — rather than one per project.

    Raises:
        LookupError: A stored code has no row in the taxonomy, which means the currency fixture is
            missing a value the portfolio depends on.
    """
    project_model = apps.get_model("portfolio", "Project")
    currency_model = apps.get_model("catalog", "Currency")

    stored_codes = set(project_model.objects.values_list("currency", flat=True))
    by_code = {row.code: row for row in currency_model.objects.filter(code__in=stored_codes)}

    missing = sorted(code for code in stored_codes if code not in by_code)
    if missing:
        message = f"catalog.Currency has no row for: {', '.join(missing)}."
        raise LookupError(message)

    for code, currency in by_code.items():
        project_model.objects.filter(currency=code).update(currency_ref=currency)


def restore_currency_codes(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Write each project's currency code back into the string column, for a reverse migration."""
    project_model = apps.get_model("portfolio", "Project")
    for project in project_model.objects.select_related("currency_ref").iterator():
        project.currency = project.currency_ref.code
        project.save(update_fields=["currency"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_currency"),
        ("portfolio", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="currency_ref",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="projects",
                to="catalog.currency",
            ),
        ),
        migrations.RunPython(resolve_currency_codes, restore_currency_codes),
        migrations.RemoveField(model_name="project", name="currency"),
        migrations.RenameField(model_name="project", old_name="currency_ref", new_name="currency"),
        migrations.AlterField(
            model_name="project",
            name="currency",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="projects",
                to="catalog.currency",
            ),
        ),
    ]
