"""The HttpOnly refresh cookie: emission, the cookie-first refresh, the CSRF header.

Ground truth: core/cookies.py, api/auth.py, api/public_signup.py.

WHY THESE ASSERT ON THE RAW `Set-Cookie` STRING. httpx's cookie jar honours the
`Secure` attribute, and the in-process client speaks `http://test` — so the jar
silently drops the very cookie under test and nothing round-trips on its own.
Reading the header and building `Cookie:` by hand is not a workaround for a test
smell here: the attributes ARE the security property (`HttpOnly` is what an XSS
cannot read, `__Host-` is what a sibling host cannot set), so asserting on the
literal wire form is exactly the point.
"""

from brain_api.config import get_settings
from brain_api.core.cookies import (
    CLIENT_HEADER_NAME,
    CLIENT_HEADER_VALUE,
    REFRESH_COOKIE_NAME,
)
from tests.test_rbac import (
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
)

WEB_CLIENT = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}


def _set_cookie_header(resp) -> str | None:
    """The `Set-Cookie` line for the refresh cookie, or None if there is none."""
    for raw in resp.headers.get_list("set-cookie"):
        if raw.startswith(REFRESH_COOKIE_NAME + "="):
            return raw
    return None


def _cookie_value(resp) -> str:
    raw = _set_cookie_header(resp)
    assert raw is not None, "no refresh cookie on the response"
    return raw.split(";", 1)[0].split("=", 1)[1]


def _sent(value: str) -> dict:
    """Headers that present `value` as the browser's refresh cookie."""
    return {"Cookie": f"{REFRESH_COOKIE_NAME}={value}"}


async def _login(client, email=OWNER_A_EMAIL, password=OWNER_A_PASSWORD):
    resp = await client.post("/auth/token", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp


# --- The cookie itself --------------------------------------------------------


async def test_login_sets_the_hardened_cookie(client):
    raw = _set_cookie_header(await _login(client))
    assert raw is not None, "login must plant the refresh cookie"
    lowered = raw.lower()
    # Each attribute is load-bearing; core/cookies.py records which attack each
    # one closes. HttpOnly is the reason this whole round exists.
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    # __Host- forbids Domain, and a Domain here would let a sibling under the
    # shared *.easypanel.host parent shadow the cookie (session fixation).
    assert "domain=" not in lowered


async def test_cookie_carries_the_same_token_as_the_body(client):
    """The body leg stays during the migration — both must be the SAME token, or a
    client trusting one and a server rotating the other disagree."""
    resp = await _login(client)
    assert _cookie_value(resp) == resp.json()["refresh_token"]


async def test_cookie_max_age_tracks_the_refresh_ttl(client):
    raw = _set_cookie_header(await _login(client))
    expected = get_settings().REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    assert f"Max-Age={expected}" in raw


async def test_login_returns_the_email_for_a_cookie_resumed_session(client):
    """A portal resuming from the cookie never saw a login form, so the address has
    to come off the wire (the access token deliberately carries no email claim)."""
    assert (await _login(client)).json()["email"] == OWNER_A_EMAIL


# --- Refreshing from the cookie ----------------------------------------------


async def test_refresh_from_cookie_with_no_body(client):
    cookie = _cookie_value(await _login(client))

    resp = await client.post("/auth/refresh", headers={**_sent(cookie), **WEB_CLIENT})
    assert resp.status_code == 200, resp.text

    # Rotation must reach the cookie too, not only the body.
    rotated = _cookie_value(resp)
    assert rotated != cookie
    assert rotated == resp.json()["refresh_token"]

    me = await client.get("/auth/me", headers=_bearer(resp.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["user"]["email"] == OWNER_A_EMAIL


async def test_refresh_prefers_the_cookie_over_the_body(client):
    """A client presenting both is one caught mid-deploy; the cookie holds the leg
    the server rotated last, so it wins — and the body's token stays unspent."""
    a_cookie = _cookie_value(await _login(client))
    b_login = await _login(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    b_body = b_login.json()["refresh_token"]

    resp = await client.post(
        "/auth/refresh",
        json={"refresh_token": b_body},
        headers={**_sent(a_cookie), **WEB_CLIENT},
    )
    assert resp.status_code == 200, resp.text
    # A's session came back, so the cookie was the credential used...
    assert resp.json()["email"] == OWNER_A_EMAIL
    # ...and B's token was never spent.
    still_b = await client.post("/auth/refresh", json={"refresh_token": b_body})
    assert still_b.status_code == 200, still_b.text


async def test_refresh_still_accepts_the_json_body_alone(client):
    """The un-migrated portal must keep working, header-less, for the whole round."""
    body_token = (await _login(client)).json()["refresh_token"]
    resp = await client.post("/auth/refresh", json={"refresh_token": body_token})
    assert resp.status_code == 200, resp.text


async def test_refresh_with_neither_leg_is_401(client):
    """401, not the 422 an unconditionally-required body would give: 'no credential'
    is an auth outcome, and the boot-time probe needs to tell 'not signed in' apart
    from 'malformed request'."""
    resp = await client.post("/auth/refresh")
    assert resp.status_code == 401, resp.text


# --- The CSRF guard -----------------------------------------------------------


async def test_cookie_refresh_without_the_client_header_is_refused(client):
    """A cross-site form POST carries the cookie but cannot set a header. This check
    is what makes such a request useless."""
    cookie = _cookie_value(await _login(client))
    resp = await client.post("/auth/refresh", headers=_sent(cookie))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "missing_client_header"


async def test_a_refused_forgery_does_not_spend_the_token(client):
    """The guard must fire BEFORE rotation — otherwise a forged request still burns
    the victim's refresh token and signs them out, which is the damage it exists to
    prevent."""
    cookie = _cookie_value(await _login(client))
    assert (await client.post("/auth/refresh", headers=_sent(cookie))).status_code == 403

    ok = await client.post("/auth/refresh", headers={**_sent(cookie), **WEB_CLIENT})
    assert ok.status_code == 200, ok.text


async def test_a_refused_forgery_does_not_clear_the_cookie(client):
    """...and it must not sign the user out the other way either, by expiring the
    cookie in the browser."""
    cookie = _cookie_value(await _login(client))
    resp = await client.post("/auth/refresh", headers=_sent(cookie))
    assert _set_cookie_header(resp) is None


async def test_wrong_header_value_is_refused_like_a_missing_one(client):
    cookie = _cookie_value(await _login(client))
    resp = await client.post(
        "/auth/refresh",
        headers={**_sent(cookie), CLIENT_HEADER_NAME: "not-web"},
    )
    assert resp.status_code == 403, resp.text


# --- A rejection clears the cookie -------------------------------------------


async def test_dead_cookie_is_expired_in_the_browser(client):
    """Left in place, a token that can never work again makes every future boot of
    that browser start with a doomed refresh."""
    resp = await client.post(
        "/auth/refresh", headers={**_sent("not-a-real-token"), **WEB_CLIENT}
    )
    assert resp.status_code == 401, resp.text
    raw = _set_cookie_header(resp)
    assert raw is not None, "a rejected cookie must be expired"
    assert "Max-Age=0" in raw or "01 Jan 1970" in raw


async def test_a_rejected_body_leg_leaves_the_cookie_alone(client):
    """The two legs can belong to different sessions in one browser; a legacy body
    rejection says nothing about the cookie."""
    resp = await client.post("/auth/refresh", json={"refresh_token": "nope"})
    assert resp.status_code == 401
    assert _set_cookie_header(resp) is None


# --- Logout -------------------------------------------------------------------


async def test_logout_from_the_cookie_revokes_and_expires_it(client):
    cookie = _cookie_value(await _login(client))

    out = await client.post("/auth/logout", headers=_sent(cookie))
    assert out.status_code == 204, out.text
    raw = _set_cookie_header(out)
    assert raw is not None and ("Max-Age=0" in raw or "01 Jan 1970" in raw)

    dead = await client.post("/auth/refresh", headers={**_sent(cookie), **WEB_CLIENT})
    assert dead.status_code == 401, "the revoked token must not still rotate"


async def test_logout_needs_no_client_header(client):
    """Deliberate asymmetry with /auth/refresh: a logout that can 403 leaves the
    cookie alive after the portal already dropped its in-memory session, and the
    next reload signs the user back in."""
    cookie = _cookie_value(await _login(client))
    out = await client.post("/auth/logout", headers=_sent(cookie))
    assert out.status_code == 204, out.text


async def test_logout_revokes_both_legs_when_both_are_presented(client):
    """A client caught mid-migration must not leave one leg alive."""
    a = _cookie_value(await _login(client))
    b = (await _login(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)).json()["refresh_token"]

    out = await client.post("/auth/logout", json={"refresh_token": b}, headers=_sent(a))
    assert out.status_code == 204, out.text

    for token, sender in ((a, "cookie"), (b, "body")):
        dead = await client.post("/auth/refresh", headers={**_sent(token), **WEB_CLIENT})
        assert dead.status_code == 401, f"the {sender} leg survived the logout"


async def test_logout_with_no_credential_at_all_is_still_204(client):
    """No token-existence oracle, and nothing left to fail on."""
    assert (await client.post("/auth/logout")).status_code == 204


# --- Every other session-minting route ---------------------------------------


async def test_registration_plants_the_cookie(client):
    """The wizard's first card IS a login; without the cookie the freshly registered
    owner loses the session on the first reload, mid-checkout."""
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Dra. Cookie",
            "email": "cookie.lead@clinica.com.br",
            "password": "leadpass1",
            "clinic_name": "Clinica Cookie",
            "whatsapp_phone": "+5511999990001",
            "catalog_ids": ["secretaria_basico"],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    assert _cookie_value(resp) == resp.json()["session"]["refresh_token"]


async def test_impersonation_never_touches_the_cookie(client):
    """Modo medico mints an access token with NO refresh leg. Overwriting the admin's
    cookie — or leaving it for the impersonated session to rotate into — would
    silently change who the browser is."""
    from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD

    admin = await _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    admin_cookie = _cookie_value(admin)

    resp = await client.post(
        "/admin/impersonate/token",
        headers={**_bearer(admin.json()["access_token"]), **_sent(admin_cookie)},
    )
    # 404 when no demo clinic is seeded — either way, no cookie may be written.
    assert _set_cookie_header(resp) is None, resp.text
