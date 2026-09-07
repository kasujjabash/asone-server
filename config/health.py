"""Is the server actually there? — the endpoint that stops silent logouts.

## The problem this exists for

When the server is down, or the internet is off, a request does not come
back at all. There is no status code and no body — the browser simply fails.
A client that treats "no response" the same as "401 Unauthorized" throws the
user's session away and shows them a login screen, and the user is left
thinking they were signed out for no reason.

**The server cannot report its own absence.** Nothing here can help once the
request has failed to arrive. What this endpoint gives the client is a cheap,
unauthenticated way to answer the follow-up question:

    a request failed
      -> can I reach /api/health/ ?
           no  -> the server or the connection is down. Say so. Keep the
                  session. Retry.
           yes -> the server is fine, so it was the session. Now the 401's
                  `code` says whether to refresh or sign out.

Without that second question a client has to guess, and the safe-looking
guess — sign them out — is the wrong one.

## Why it checks the database

A Django process that is running but cannot reach Postgres answers every
real request with a 500. Reporting that as healthy would send a client into
a retry loop against a server that cannot serve. "Up" has to mean "able to
do the job".

Deliberately unauthenticated: a client that cannot authenticate is exactly
the one asking. It reveals nothing beyond the fact that a server exists,
which anybody who can reach the port already knows.
"""

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField(help_text='"ok" or "degraded".')
    database = serializers.BooleanField(help_text="Whether Postgres answered.")


@extend_schema(
    tags=["Health"],
    summary="Is the server up?",
    responses={200: HealthSerializer, 503: HealthSerializer},
    auth=[],
    description=(
        "Cheap, unauthenticated liveness check.\n\n"
        "**Use it to tell a network failure from an expired session.** When "
        "a request fails with no response, call this: if it also fails, the "
        "server or the connection is down — tell the user that and **keep "
        "their session**. If it succeeds, the problem was authentication, "
        "and the 401's `code` says whether to refresh (`token_expired`) or "
        "sign out (`token_invalid`).\n\n"
        "Returns **503** if the database is unreachable. A server that "
        "cannot reach Postgres answers every real request with a 500, so "
        "reporting it as healthy would send clients into a retry loop."
    ),
)
class HealthView(APIView):
    """No authentication, no throttle scope, one trivial query."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            database = True
        except Exception:
            # Deliberately broad. Whatever went wrong with the database, the
            # answer to "are you healthy" is no — and this endpoint must
            # never itself be the thing that 500s.
            database = False

        return Response(
            {"status": "ok" if database else "degraded", "database": database},
            status=status.HTTP_200_OK if database else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
