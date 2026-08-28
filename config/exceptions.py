"""Project-wide DRF exception handling.

Django models raise `django.core.exceptions.ValidationError` from
`full_clean()`. DRF does not know that class — it only handles its own — so
without this handler a model-level rule reaches the client as a 500 Internal
Server Error, which reads as "the server is broken" rather than "your input
was rejected".

That matters here because the invariants worth having are enforced on the
models and in the database: an overlapping price period, a warehouse user
with no warehouse. Those are all 400s.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import ProtectedError, RestrictedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    """Translate Django's database-level refusals, then defer to DRF."""
    if isinstance(exc, DjangoValidationError):
        return Response(_as_detail(exc), status=status.HTTP_400_BAD_REQUEST)

    if isinstance(exc, (ProtectedError, RestrictedError)):
        return Response(_as_conflict(exc), status=status.HTTP_409_CONFLICT)

    return drf_exception_handler(exc, context)


def _as_conflict(exc):
    """Explain a refused delete in terms of what is still using the row.

    409 rather than 400: the request was well formed, and nothing the caller
    can change about it would help. Something else has to stop referencing
    the row first — usually by being deleted or reassigned. A distinct status
    lets a client say "still in use" rather than "invalid".

    Django names the protecting objects, which is the useful part: "cannot
    delete Namayemba" is far less help than "Namayemba PS still points at it".
    """
    blockers = sorted({str(obj) for obj in exc.protected_objects})
    shown, remaining = blockers[:5], len(blockers) - 5

    detail = "This cannot be deleted while other records still refer to it."
    if shown:
        listed = ", ".join(shown)
        if remaining > 0:
            listed += f", and {remaining} more"
        detail = f"{detail} Still in use by: {listed}."

    return {"detail": detail, "in_use_by": blockers[:50]}


def _as_detail(exc: DjangoValidationError):
    """Shape a Django ValidationError like a DRF one.

    Field errors keep their field names so a form can highlight them.
    Model-wide errors (Django files these under `__all__`) become
    `non_field_errors`, which is what DRF clients already look for.
    """
    if hasattr(exc, "error_dict"):
        errors = {field: list(messages) for field, messages in exc.message_dict.items()}
        if "__all__" in errors:
            errors["non_field_errors"] = errors.pop("__all__")
        return errors

    return {"non_field_errors": list(exc.messages)}
