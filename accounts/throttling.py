"""Rate limits for the endpoints that accept a password.

Sizing these for AsOne needs one fact about the deployment: every warehouse
and school is a rural site on a single shared internet connection, so every
member of staff there reaches the server from the *same* public IP address.

A plain per-IP limit on login therefore punishes the wrong person. Ten staff
signing in at eight in the morning, plus the usual handful of typos, looks
identical to one attacker guessing — and the site gets locked out.

So login is limited on two separate axes:

    LoginRateThrottle       per email     — stops guessing at one account
    LoginBurstRateThrottle  per IP        — stops spraying many accounts,
                                            set high enough that a whole
                                            warehouse signing in is fine

Both must pass. Neither alone is sufficient: the first would let an attacker
spray a thousand addresses from one connection, the second would let them hammer
one account from a botnet.
"""

from rest_framework.throttling import SimpleRateThrottle


class LoginRateThrottle(SimpleRateThrottle):
    """Limit attempts against a single account, whoever they come from.

    Keyed on the submitted email address rather than the caller, so one person
    fat-fingering their password cannot lock out the colleague beside them
    on the same connection.

    The address is only ever used to build a cache key — it is not trusted
    and never reaches the database from here. A caller who submits no address
    at all falls back to their IP, so the endpoint cannot be flooded with
    empty requests for free.
    """

    scope = "login"

    def get_cache_key(self, request, view):
        email = request.data.get("email") if hasattr(request, "data") else None
        if isinstance(email, str) and email.strip():
            # Lower-cased so Julius@AsOne.test and julius@asone.test share a
            # bucket, and length capped so a huge string cannot bloat the key.
            ident = email.strip().lower()[:254]
        else:
            ident = self.get_ident(request)

        return self.cache_format % {"scope": self.scope, "ident": ident}


class LoginBurstRateThrottle(SimpleRateThrottle):
    """Limit total login attempts from one address, across all accounts.

    Deliberately generous. This is the backstop against someone working
    through a list of addresses, not the primary defence — it has to leave
    room for an entire site to sign in at the start of the day.
    """

    scope = "login_burst"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
