"""Password-reset round tests: the flow brain-api never had.

Ground truth: api/auth.py (`/auth/password-reset/*`), services/auth.py
(`issue_password_reset_token` / `find_password_reset_user` / `complete_password_reset`),
schemas/auth.py, models/user.py (`reset_token_hash` / `reset_token_expires_at`).

WHY THIS EXISTS: before this round brain-api had no reset capability at all, and
brain-frontend's "Esqueci a senha" screens were calling PreCheck's API instead. For any
user that exists only in brain-api — i.e. every self-serve `/cadastro` signup — PreCheck
found no such email and, by its own (correct) anti-enumeration rule, answered a generic
success and sent nothing. The failure was 100% silent. The end-to-end test below is
therefore written against exactly that case: a user seeded in brain-api that never
existed in PreCheck.
"""

from urllib.parse import parse_qs, urlparse

import pytest

import brain_api.api.auth as auth_api
from brain_api.config import get_settings
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD

NEW_PASSWORD = "novaSenha123"


class _EmailSpy:
    """Captures calls to secretaria_provisioning.send_notification_email.

    The real sender POSTs to secretaria's `/internal/notifications/email`, which looks the
    template up in `services/email.py::_TEMPLATES` and, on a miss, logs
    `transactional_email_unknown_template` and returns False — a silent no-send. So the
    template NAME is load-bearing across a repo boundary, and asserting it here is what
    keeps this flow from regressing into the same silent failure it was written to fix.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, to: str, template: str, variables: dict) -> bool:
        self.calls.append((to, template, variables))
        return True

    @property
    def last(self) -> tuple[str, str, dict]:
        assert self.calls, "expected send_notification_email to have been called"
        return self.calls[-1]

    def token_from_link(self) -> str:
        """Pull the raw token out of the emailed link, the way a user's browser would."""
        link = self.last[2]["link"]
        return parse_qs(urlparse(link).query)["token"][0]


@pytest.fixture
def email_spy(monkeypatch):
    spy = _EmailSpy()
    monkeypatch.setattr(auth_api.secretaria_provisioning, "send_notification_email", spy)
    return spy


# --- The end-to-end path ----------------------------------------------------


async def test_reset_end_to_end_for_a_brain_api_only_user(client, email_spy):
    """request -> emailed link -> verify -> confirm -> log in with the NEW password.

    This is the exact journey that silently did nothing before this round.
    """
    # 1. Ask for the reset.
    r = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    assert r.status_code == 200

    # The email went out, to the right address, on the template secretaria knows.
    to, template, variables = email_spy.last
    assert to == OWNER_A_EMAIL
    assert template == "password_reset"
    # Every placeholder secretaria's template renders must be supplied, or the body
    # silently ships with a literal "{link}" in it (_SafeDict swallows the KeyError).
    assert set(variables) >= {"name", "link", "ttl_minutes"}
    assert variables["ttl_minutes"] == get_settings().PASSWORD_RESET_TOKEN_EXPIRE_MINUTES

    # 2. The link points at the reset screen and carries a usable token.
    link = variables["link"]
    assert link.startswith(get_settings().FRONTEND_BASE_URL)
    assert "/esqueci_senha/token?token=" in link
    raw_token = email_spy.token_from_link()

    # 3. Pre-flight passes and does NOT consume the token.
    r = await client.post("/auth/password-reset/verify", json={"token": raw_token})
    assert r.status_code == 200

    # 4. Consume it.
    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200

    # 5. The whole point: the NEW password logs in...
    r = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": NEW_PASSWORD}
    )
    assert r.status_code == 200
    assert r.json()["access_token"]

    # ...and the OLD one no longer does.
    r = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    assert r.status_code == 401


# --- Enumeration --------------------------------------------------------------


async def test_unknown_email_is_indistinguishable_from_a_real_one(client, email_spy):
    """Same status AND same body for a registered and an unregistered address.

    If these ever diverge, the endpoint becomes an oracle for "is this clinic a
    customer?" — answerable by anyone, unauthenticated.
    """
    real = await client.post(
        "/auth/password-reset/request", json={"email": OWNER_A_EMAIL}
    )
    unknown = await client.post(
        "/auth/password-reset/request",
        json={"email": "nobody-here@nao-existe-mesmo.com.br"},
    )

    assert real.status_code == unknown.status_code == 200
    assert real.json() == unknown.json()
    # ...while only the real one actually sent anything.
    assert [c[0] for c in email_spy.calls] == [OWNER_A_EMAIL]


async def test_malformed_email_422_is_not_an_enumeration_leak(client, email_spy):
    """A syntactically invalid address 422s instead of getting the generic 200.

    Pinned deliberately, because it LOOKS like the enumeration hole the endpoint exists
    to avoid and it is not: `EmailStr` rejects on FORMAT alone, before the handler runs,
    so the answer depends only on what was typed and never on whether an account exists.
    A well-formed unregistered address still gets the same 200 as a real one (asserted
    above). Do not "fix" this by swallowing validation errors into the generic response —
    that would hide typos from users for no security gain.

    (`.invalid` is one of the special-use TLDs email-validator refuses outright, which is
    how this was discovered.)
    """
    r = await client.post(
        "/auth/password-reset/request", json={"email": "nobody-here@example.invalid"}
    )
    assert r.status_code == 422
    assert email_spy.calls == []


async def test_request_is_case_insensitive_on_email(client, email_spy):
    """Users are stored lower-cased; a capitalised address must still find them —
    otherwise a user who types their email the way their phone capitalises it gets the
    generic success and no email, which is the original bug wearing a different hat."""
    r = await client.post(
        "/auth/password-reset/request", json={"email": OWNER_A_EMAIL.upper()}
    )
    assert r.status_code == 200
    assert email_spy.last[0] == OWNER_A_EMAIL


# --- Token lifecycle ----------------------------------------------------------


async def test_token_is_single_use(client, email_spy):
    """Confirm burns the token — a replayed link cannot set a second password."""
    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    raw_token = email_spy.token_from_link()

    first = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": NEW_PASSWORD},
    )
    assert first.status_code == 200

    replay = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "outraSenha456"},
    )
    assert replay.status_code == 400
    # And the replay did NOT change the password.
    r = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": NEW_PASSWORD}
    )
    assert r.status_code == 200


async def test_requesting_again_invalidates_the_previous_link(client, email_spy):
    """Only one live link per user: the second request overwrites the stored hash.

    Matters because a user who clicks "resend" then opens the OLDER email must not be
    able to set a password with a token they may have already leaked.
    """
    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    first_token = email_spy.token_from_link()

    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    second_token = email_spy.token_from_link()
    assert first_token != second_token

    stale = await client.post(
        "/auth/password-reset/confirm",
        json={"token": first_token, "new_password": NEW_PASSWORD},
    )
    assert stale.status_code == 400

    fresh = await client.post(
        "/auth/password-reset/confirm",
        json={"token": second_token, "new_password": NEW_PASSWORD},
    )
    assert fresh.status_code == 200


async def test_expired_token_is_rejected(client, email_spy, monkeypatch):
    """A token minted with a non-positive TTL is already past its expiry."""
    monkeypatch.setattr(get_settings(), "PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", -1)
    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    raw_token = email_spy.token_from_link()

    assert (
        await client.post("/auth/password-reset/verify", json={"token": raw_token})
    ).status_code == 400
    assert (
        await client.post(
            "/auth/password-reset/confirm",
            json={"token": raw_token, "new_password": NEW_PASSWORD},
        )
    ).status_code == 400


async def test_unknown_token_is_rejected_on_both_steps(client):
    """Same 400 for a token that never existed as for one that expired — the response
    must not reveal which."""
    bogus = {"token": "a" * 43}
    assert (await client.post("/auth/password-reset/verify", json=bogus)).status_code == 400
    assert (
        await client.post(
            "/auth/password-reset/confirm", json={**bogus, "new_password": NEW_PASSWORD}
        )
    ).status_code == 400


# --- Password policy ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad_password",
    [
        "curta1",  # < 8 chars
        "semdigitos",  # no digit
        "12345678",  # no letter
        "a1" + "x" * 71,  # > 72 bytes (bcrypt's ceiling)
    ],
)
async def test_confirm_enforces_the_signup_password_policy(client, email_spy, bad_password):
    """Reset is not a back door around the rule the account was created under —
    same 8-72 + letter + digit policy as SignupIntentCreate / SetPasswordIn."""
    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    raw_token = email_spy.token_from_link()

    r = await client.post(
        "/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": bad_password},
    )
    assert r.status_code == 422

    # The token survived a rejected attempt — the user can retry with a valid password
    # instead of having to request a whole new email.
    assert (
        await client.post("/auth/password-reset/verify", json={"token": raw_token})
    ).status_code == 200


# --- Rate limiting ------------------------------------------------------------


async def test_request_is_rate_limited(client, email_spy, monkeypatch):
    """The request route sends email to a caller-chosen address, so it shares the
    per-IP auth budget with /token and /refresh (conftest disables it by default)."""
    monkeypatch.setattr(auth_api._limiter, "_limit_getter", lambda: 2)

    ok_1 = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    ok_2 = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    blocked = await client.post(
        "/auth/password-reset/request", json={"email": OWNER_A_EMAIL}
    )

    assert ok_1.status_code == 200
    assert ok_2.status_code == 200
    assert blocked.status_code == 429
