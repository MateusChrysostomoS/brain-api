"""The HttpOnly refresh-token cookie, and the CSRF header that guards it.

WHY THIS EXISTS. Until 2026-08-31 the browser kept BOTH legs of the session —
the access JWT and the opaque refresh token — in `sessionStorage` under
`brain.session`, i.e. in a store any script on the page can read. One XSS on any
screen of either portal handed the attacker a 14-day revocable-but-unrevoked
credential, which is strictly worse than stealing a 30-minute access token. The
refresh leg now travels in a cookie the page's own JavaScript cannot read.

THE THREE ATTRIBUTES, AND WHY EACH ONE IS NOT NEGOTIABLE

`HttpOnly`  — the whole point: `document.cookie` cannot see it, so an injected
              script cannot exfiltrate it.
`Secure`    — required by the `__Host-` prefix, and required anyway: the cookie
              must never ride a plain-http request.
`SameSite=Lax` — NOT `None`. A cross-site `<form>` POST therefore never carries
              it. `None` would have been needed only if the browser still saw
              brain-api as a THIRD party; it does not, because each frontend now
              reverse-proxies brain-api under its own origin at `/api/*` (see
              the `static-export-nginx-hardening` skill). That same proxy is
              what keeps Safari/Firefox tracking prevention from silently
              evicting the cookie mid-session, which is the failure mode a naive
              cross-origin `SameSite=None` cookie would have shipped.

THE `__Host-` PREFIX. A browser accepts a `__Host-`-prefixed cookie only when it
is `Secure`, has `Path=/`, and carries NO `Domain` attribute — which pins it to
exactly one host and makes it impossible for a sibling host to set. That matters
concretely here: every service in this mesh is deployed under a SHARED parent
domain (`*.cpux9k.easypanel.host`). Without the prefix, a neighbour under that
parent could set `Domain=<parent>` and shadow this cookie with one of its own —
session fixation. With it, the only writer is this exact host.

The prefix is also why `Path` cannot be narrowed to `/api/auth`: `__Host-`
mandates `Path=/`. That costs nothing (the cookie is unreadable by JS and the
proxy is same-origin) and buys mount-point independence — brain-api never has to
know WHERE the frontend proxied it.

THE CSRF HEADER, AND WHY `SameSite` ALONE IS NOT THE ANSWER HERE. `SameSite`
compares *sites*, i.e. registrable domains — and whether `easypanel.host` is a
public suffix is not something this code can assume. If it is not, every app
under that parent counts as same-site and `Lax` protects nothing against a
neighbour. So the load-bearing layer is `X-Brain-Client: web`: a cross-site
`<form>`, `<img>` or navigation cannot set a request header at all, and a
cross-site `fetch()` that tries triggers a CORS preflight that this service
answers only for the origins in `CORS_ALLOW_ORIGINS`. `SameSite=Lax` is then the
free second layer, not the first.

The header is required ONLY where the cookie is the credential (see
`api/auth.py`). Every other authenticated route reads a bearer `Authorization`
header, which a cross-site request cannot forge either — demanding the header
there would break the mesh's server-to-server callers, Stripe's webhook and the
not-yet-migrated frontend for no gain.
"""

from fastapi import HTTPException, Request, Response, status

from brain_api.config import get_settings

# `__Host-` is part of the NAME, not a flag: the browser enforces the prefix's
# rules on set, and sends it back under this exact string.
REFRESH_COOKIE_NAME = "__Host-refresh_token"

# The header a browser client must send when it authenticates with the cookie.
# Value is a constant, not a secret — its security comes from the fact that only
# same-origin JavaScript can set a custom header at all.
CLIENT_HEADER_NAME = "X-Brain-Client"
CLIENT_HEADER_VALUE = "web"

_SECONDS_PER_DAY = 24 * 60 * 60


def set_refresh_cookie(response: Response, raw_token: str) -> None:
    """Attach the refresh token to `response` as the hardened cookie.

    Called by every route that mints a session pair, so the cookie always follows
    rotation: a refresh writes the SUCCESSOR token here, never the one just spent.
    """
    settings = get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_token,
        # Persistent by default so a reload — or a new tab — resumes the session
        # instead of asking a doctor mid-consultation to log in again. Flip
        # REFRESH_COOKIE_PERSISTENT to false to emit a SESSION cookie instead,
        # which dies with the browser process: the right trade for a clinic that
        # shares a reception-desk machine between people.
        max_age=(
            settings.REFRESH_TOKEN_EXPIRE_DAYS * _SECONDS_PER_DAY
            if settings.REFRESH_COOKIE_PERSISTENT
            else None
        ),
        path="/",  # mandated by the __Host- prefix
        domain=None,  # ditto — host-locked, never sent to a sibling
        secure=True,  # ditto
        httponly=True,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response) -> None:
    """Expire the cookie in the browser (logout, or a rejected rotation).

    The attributes must MATCH the ones used to set it or the browser keeps the
    original: a delete is really a set with an expiry in the past, and `Path`,
    `Secure` and the `__Host-` rules are all part of the cookie's identity.
    """
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path="/",
        domain=None,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def read_refresh_cookie(request: Request) -> str | None:
    """The refresh token the browser sent, or None. Never logged by callers."""
    value = request.cookies.get(REFRESH_COOKIE_NAME)
    return value or None


def require_client_header(request: Request) -> None:
    """403 unless the caller proved it is our own same-origin JavaScript.

    Guards the routes where the COOKIE is the credential — an ambient one the
    browser attaches on its own, which is exactly what makes CSRF possible. Fails
    CLOSED and says nothing useful: a forged request learns only that it was
    refused.
    """
    if request.headers.get(CLIENT_HEADER_NAME) != CLIENT_HEADER_VALUE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing_client_header",
        )
