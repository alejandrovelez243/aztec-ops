"""Admin registration for the person.

Registered through Django's own :class:`~django.contrib.auth.admin.UserAdmin`, extended rather
than replaced, so password hashing, the change-password link and the permission widgets keep
working. Registering a user model with a plain ``ModelAdmin`` silently turns the password field
into an editable plain-text box.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.accounts.models import User

#: The operational identity of a person, as opposed to the credentials ``UserAdmin`` already
#: renders. Inserted into both the change and the add form so a person created from the admin
#: cannot exist without the code every payload refers to them by.
_IDENTITY_FIELDS = ("code", "alias", "role", "weekly_capacity_points")


@admin.register(User)
class PersonAdmin(UserAdmin[User]):
    """Admin for :class:`~apps.accounts.models.User`.

    Subclasses ``UserAdmin`` instead of defining a fresh ``ModelAdmin``: the parent owns the
    password handling, and the only thing this class adds is the operational half of a person.
    """

    # ``UserAdmin.fieldsets`` is declared nullable (a subclass may drop the credential groups
    # entirely), so it is spread through a fallback rather than unpacked blind.
    fieldsets = [
        *(UserAdmin.fieldsets or ()),
        ("Operation", {"fields": _IDENTITY_FIELDS}),
    ]
    add_fieldsets = [
        *(UserAdmin.add_fieldsets or ()),
        ("Operation", {"fields": _IDENTITY_FIELDS}),
    ]
    list_display = ("code", "alias", "role", "weekly_capacity_points", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("code", "alias", "username", "email")
    ordering = ("alias",)
    list_select_related = ("role",)
