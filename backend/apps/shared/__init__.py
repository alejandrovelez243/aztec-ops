"""Shared kernel: the few pure value types that belong to no bounded context.

This package is **not** a Django app and is deliberately absent from ``INSTALLED_APPS``. It holds
nothing but frozen Pydantic models with no Django import, so any context's ``domain/`` may import
it without acquiring a dependency on another context (ARCHITECTURE §7).

Nothing with a business rule may be added here. The moment a type in this package needs to know
what a project, a task or a workflow is, it belongs to that context instead — a shared kernel that
grows business logic is how two contexts end up coupled through a module nobody owns.
"""
