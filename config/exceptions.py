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
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    """Translate Django's ValidationError into a DRF 400, then defer to DRF."""
    if isinstance(exc, DjangoValidationError):
        return Response(_as_detail(exc), status=status.HTTP_400_BAD_REQUEST)

    return drf_exception_handler(exc, context)


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
