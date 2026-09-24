"""Attachments on the Brain-Message channel — the brain-api edge (2026-09-18).

Part 1 of the chain in z_prompts/PLANO_PORTAL_API_MVP.md. A file a patient sends on the Portal
is judged HERE (its real type from its bytes, its real size, a safe name), relayed to
secretarIA as multipart, referenced in the transcript without any storage detail, and handed
back only through a route that streams it under the patient's own session.

secretarIA's half (part 2) does not exist yet, so every upstream below is a fake at the WIRE
level — patched on `httpx.AsyncClient.send`, below `request` — and what is asserted is the body
httpx really encoded, not the arguments it was handed (docs/CHECKPOINT_brain_message_anexos.md).
"""

import json
import logging
import re
import struct
import uuid
import zlib

import httpx
import pytest

from brain_api.core import attachments
from tests import test_patient_access as base

# The seeded three-clinic fixture, re-bound (not imported) so using it as a parameter below
# is not a redefinition of an unused import.
pclient = base.pclient
_bearer = base._bearer
_configure_mesh = base._configure_mesh
_login = base._login

SECRETARIA_MESSAGES = "/patient-access/threads/secretaria/messages"
PRECHECK_MESSAGES = "/patient-access/threads/precheck/messages"
_PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def _png(text: bytes = b"") -> bytes:
    """A real, decodable 2x2 PNG — optionally carrying `text` in a tEXt chunk."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + (chunk(b"tEXt", b"Comment\x00" + text) if text else b"")
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# The greeting hop (2026-09-19, TASK-003 §2) is a background side effect of the `POST /pending`
# SETUP step these tests use — one per created visit, finished before the upload is even sent.
# It is answered here WITHOUT being recorded: counting it would make every "exactly N relays"
# assertion below depend on an unrelated feature, and letting it reach `real_send` would make
# each test wait on a real connection attempt to a host that does not exist. Where the greeting
# itself is asserted is `tests/test_portal_auto_greeting.py`.
_GREETING_PATH = "/internal/brain-message/open"


def _mesh(monkeypatch, answer):
    """Serve every mesh hop from `answer(request) -> httpx.Response`, at the wire level.

    The test client's own hop and the OTP e-mail (same secretarIA base URL, fail-soft) pass
    through untouched, and the greeting is stubbed out, so "nothing was relayed" assertions
    count switchboard hops only.
    """
    calls: list[dict] = []
    real_send = httpx.AsyncClient.send

    async def _fake_send(self, request, **kwargs):
        url = str(request.url)
        mesh = "secretaria:8000" in url or "precheck:8000" in url
        if not mesh or url.endswith("/internal/notifications/email"):
            return await real_send(self, request, **kwargs)
        if request.url.path == _GREETING_PATH:
            return httpx.Response(202, json={"status": "queued"}, request=request)
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "headers": request.headers,
                "body": await request.aread(),
            }
        )
        return answer(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
    return calls


def _queued(request):
    return httpx.Response(202, json={"status": "queued"}, request=request)


def _wire_parts(call) -> dict[str, dict]:
    """The multipart body httpx put on the wire, split by RFC 7578 rules."""
    content_type = call["headers"]["content-type"]
    assert content_type.startswith("multipart/form-data; boundary="), content_type
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = {}
    for raw in call["body"].split(b"--" + boundary)[1:-1]:
        head, _, content = raw[2:].partition(b"\r\n\r\n")
        headers = head.decode()
        filename = re.search(r'; filename="([^"]*)"', headers)
        part_type = re.search(r"Content-Type: (\S+)", headers)
        parts[re.search(r'; name="([^"]*)"', headers).group(1)] = {
            "filename": filename.group(1) if filename else None,
            "content_type": part_type.group(1) if part_type else None,
            "content": content[:-2],  # the CRLF before the next boundary
        }
    return parts


async def _patient(pclient, email="paciente@exemplo.com") -> dict:
    client, sessionmaker, seed = pclient
    return (await _login(client, sessionmaker, seed.both, email)).json()


async def _upload(client, token, name, content, *, url=SECRETARIA_MESSAGES, **fields):
    return await client.post(
        url,
        headers=_bearer(token),
        data=fields or None,
        files={"file": (name, content, "image/jpeg")},  # a browser's claim, never trusted
    )


# --- 1) A real file goes through, typed by its bytes ------------------------------------------


async def test_a_real_image_is_relayed_to_secretaria_as_multipart_typed_by_its_bytes(
    pclient, monkeypatch
):
    """Checklist 1: a REAL PNG, sent with the browser's wrong claims (`.jpg`, `image/jpeg`),
    reaches secretarIA's inbound as multipart, byte for byte, typed by its CONTENT, with the
    tenant and the handle taken off the session."""
    client, _, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    login = await _patient(pclient)
    image = _png()

    resp = await _upload(
        client,
        login["access_token"],
        "foto-da-receita.jpg",
        image,
        text="Segue a foto da receita",
        patient_name="Maria",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["product"] == "secretaria"
    assert resp.json()["payload"] == {"status": "queued"}
    assert len(calls) == 1
    call = calls[0]
    assert (call["method"], call["path"]) == ("POST", "/internal/brain-message/inbound")
    assert call["headers"]["x-internal-api-key"] == "secretaria-key-AAA"
    assert "x-internal-token" not in call["headers"]
    # Framed by length, not chunked, although the body streamed through a worker thread.
    assert call["headers"]["content-length"] == str(len(call["body"]))
    assert "transfer-encoding" not in call["headers"]
    parts = _wire_parts(call)
    assert set(parts) == {"tenant_id", "external_id", "text", "patient_name", "file"}
    assert parts["tenant_id"]["content"] == str(seed.both).encode()
    assert parts["external_id"]["content"] == login["patient_ref"].encode()
    assert parts["text"]["content"] == b"Segue a foto da receita"
    assert parts["patient_name"]["content"] == b"Maria"
    assert parts["file"]["content_type"] == "image/png"  # the bytes, not "image/jpeg"
    assert parts["file"]["filename"] == "foto-da-receita.png"  # a name that agrees
    assert parts["file"]["content"] == image  # never re-encoded


@pytest.mark.parametrize(
    ("name", "content", "expected_type", "expected_name"),
    [
        ("receita.pdf", _PDF, "application/pdf", "receita.pdf"),
        ("foto.JPEG", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(32), "image/jpeg", "foto.jpeg"),
        ("anim.gif", b"GIF89a" + bytes(32), "image/gif", "anim.gif"),
        ("tela.webp", b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(32), "image/webp", "tela.webp"),
        ("sem-extensao", _png(), "image/png", "sem-extensao.png"),
    ],
)
async def test_every_accepted_kind_travels_captionless_as_its_sniffed_type(
    pclient, monkeypatch, name, content, expected_type, expected_name
):
    """A file with no caption is a whole message; each accepted kind keeps its real type."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    login = await _patient(pclient)

    resp = await _upload(client, login["access_token"], name, content)

    assert resp.status_code == 200, resp.text
    parts = _wire_parts(calls[0])
    assert set(parts) == {"tenant_id", "external_id", "file"}  # no empty caption relayed
    assert parts["file"]["content_type"] == expected_type
    assert parts["file"]["filename"] == expected_name
    assert parts["file"]["content"] == content


# --- 2) PreCheck takes an exam on its own wire: raw bytes, addressed by query (2026-09-22) ------

_PRECHECK_TURN = {"messages": [], "status": "question", "state": "MEDIA_WAITING"}


def _precheck_turn(request):
    return httpx.Response(200, json=_PRECHECK_TURN, request=request)


async def test_an_exam_is_relayed_to_precheck_as_its_raw_bytes(pclient, monkeypatch):
    """The whole wire: PreCheck's route and header, the handle off the session as a QUERY, the
    body = the file itself typed by its bytes, framed by length — and no caption or name, which
    in a query string would be PII in every access log."""
    client, _, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _precheck_turn)
    login = await _patient(pclient)
    image = _png()

    resp = await _upload(
        client,
        login["access_token"],
        "exame.jpg",
        image,
        url=PRECHECK_MESSAGES,
        text="meu exame",
        patient_name="Maria",
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["product"] == "precheck"
    assert resp.json()["payload"] == _PRECHECK_TURN
    (call,) = calls
    assert (call["method"], call["path"]) == ("POST", "/internal/brain-message/inbound-media")
    assert call["params"] == {"tenant_id": str(seed.both), "session_ref": login["patient_ref"]}
    assert call["headers"]["x-internal-token"] == "precheck-token-BBB"
    assert "x-internal-api-key" not in call["headers"]
    assert call["headers"]["content-type"] == "image/png"  # the bytes, not the browser's claim
    assert call["headers"]["content-length"] == str(len(image))
    assert "transfer-encoding" not in call["headers"]
    assert call["body"] == image  # never re-encoded, never wrapped


async def test_a_pdf_bigger_than_one_chunk_reaches_precheck_whole(pclient, monkeypatch):
    """Several read chunks, one body: nothing lost or duplicated at a chunk boundary."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _precheck_turn)
    login = await _patient(pclient)
    pdf = _PDF + bytes(range(256)) * 1024  # ~256 KiB, four 64 KiB chunks and change

    resp = await _upload(client, login["access_token"], "laudo.pdf", pdf, url=PRECHECK_MESSAGES)

    assert resp.status_code == 200, resp.text
    assert calls[0]["headers"]["content-type"] == "application/pdf"
    assert calls[0]["body"] == pdf


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("attachment_consent_required", 409),  # before the LGPD terms were accepted
        ("attachment_unsupported_for_product", 422),  # a clinic with no exam pipeline
    ],
)
async def test_precheck_refusals_reach_the_patient_in_this_services_words(
    pclient, monkeypatch, code, status_code
):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(
        monkeypatch, _refusing(status_code, {"code": code, "message": _UPSTREAM_SENTENCE})
    )
    login = await _patient(pclient)

    resp = await _upload(client, login["access_token"], "exame.png", _png(), url=PRECHECK_MESSAGES)

    assert resp.status_code == status_code, resp.text
    assert resp.json() == {"detail": {"code": code, "message": attachments.REFUSALS[code][1]}}
    assert _UPSTREAM_SENTENCE not in resp.text
    assert len(calls) == 1


async def test_any_other_precheck_refusal_stays_the_opaque_product_error(pclient, monkeypatch):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    _mesh(monkeypatch, _refusing(422, {"code": "something_else", "message": _UPSTREAM_SENTENCE}))
    login = await _patient(pclient)

    resp = await _upload(client, login["access_token"], "exame.png", _png(), url=PRECHECK_MESSAGES)

    assert resp.status_code == 502, resp.text
    assert resp.json() == {"detail": "product_error"}


async def test_precheck_json_still_refuses_a_file_shaped_field(pclient, monkeypatch):
    """A file travels only as multipart; the JSON contract is unchanged there."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _precheck_turn)
    login = await _patient(pclient)

    resp = await client.post(
        PRECHECK_MESSAGES,
        headers=_bearer(login["access_token"]),
        json={"text": "oi", "attachment": {"filename": "exame.pdf"}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "extra_forbidden"
    assert calls == []


# --- 3) Clear refusals: type, name, emptiness, size --------------------------------------------


@pytest.mark.parametrize(
    ("name", "content", "status_code", "code"),
    [
        pytest.param(
            "receita.jpg",
            b"<html><script>alert(1)</script></html>",
            415,
            "attachment_type_unsupported",
            id="jpg-that-is-html",
        ),
        pytest.param("exame.jpg", _PDF, 422, "attachment_type_mismatch", id="jpg-that-is-a-pdf"),
        pytest.param("foto.png", b"BM" + bytes(64), 415, "attachment_type_unsupported", id="bmp"),
        pytest.param(
            "logo.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            415,
            "attachment_type_unsupported",
            id="svg",
        ),
        pytest.param("foto.exe", _png(), 422, "attachment_type_mismatch", id="png-named-exe"),
        pytest.param("vazio.png", b"", 422, "attachment_empty", id="empty"),
    ],
)
async def test_a_file_that_is_not_what_it_claims_is_refused_clearly(
    pclient, monkeypatch, name, content, status_code, code
):
    """Checklist 3: a 4xx with a stable code and a sentence to show — never a 5xx, never a relay."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    login = await _patient(pclient)

    resp = await _upload(client, login["access_token"], name, content)

    assert resp.status_code == status_code, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert detail["message"] == attachments.REFUSALS[code][1]
    assert calls == []


async def test_one_byte_over_twenty_mib_is_refused_at_the_real_ceiling(pclient, monkeypatch):
    """Checklist 3, size — against the REAL constant, not a patched one."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    login = await _patient(pclient)
    image = _png()
    too_big = image + bytes(attachments.MAX_ATTACHMENT_BYTES + 1 - len(image))

    resp = await _upload(client, login["access_token"], "grande.png", too_big)

    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"]["code"] == "attachment_too_large"
    assert resp.json()["detail"]["max_bytes"] == 20 * 1024 * 1024 == 20971520
    assert calls == []


async def test_the_ceiling_is_inclusive_and_an_oversized_body_is_cut_before_parsing(
    pclient, monkeypatch
):
    """At the ceiling passes; one byte over fails; a body far over the cap fails on its
    Content-Length, and a CHUNKED one (no Content-Length) fails while it streams."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 1024)
    token = (await _patient(pclient))["access_token"]
    image = _png()
    exact = image + bytes(1024 - len(image))

    assert (await _upload(client, token, "a.png", exact)).status_code == 200
    assert (await _upload(client, token, "a.png", exact + b"\x00")).status_code == 413
    huge = await _upload(client, token, "a.png", image + bytes(200 * 1024))
    assert huge.status_code == 413
    assert huge.json()["detail"]["code"] == "attachment_too_large"

    boundary = "brain-test-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + image
        + bytes(200 * 1024)
        + f"\r\n--{boundary}--\r\n".encode()
    )

    async def chunked():
        for start in range(0, len(body), 16 * 1024):
            yield body[start : start + 16 * 1024]

    resp = await client.post(
        SECRETARIA_MESSAGES,
        headers={**_bearer(token), "content-type": f"multipart/form-data; boundary={boundary}"},
        content=chunked(),
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"]["code"] == "attachment_too_large"
    assert len(calls) == 1  # only the file exactly at the ceiling


async def test_a_malformed_multipart_is_one_clear_refusal(pclient, monkeypatch):
    """Two files, a file under another name, or a field that names a tenant: never relayed."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    token = (await _patient(pclient))["access_token"]
    headers = _bearer(token)

    two = await client.post(
        SECRETARIA_MESSAGES,
        headers=headers,
        files=[("file", ("a.png", _png(), "image/png")), ("file", ("b.png", _png(), "image/png"))],
    )
    assert two.status_code == 422 and two.json()["detail"]["code"] == "attachment_malformed"
    renamed = await client.post(
        SECRETARIA_MESSAGES, headers=headers, files={"anexo": ("a.png", _png(), "image/png")}
    )
    assert renamed.status_code == 422
    assert renamed.json()["detail"]["code"] == "attachment_malformed"
    aimed = await _upload(client, token, "a.png", _png(), tenant_id=str(uuid.uuid4()))
    assert aimed.status_code == 422
    assert aimed.json()["detail"][0]["loc"] == ["body", "tenant_id"]
    # A part header python-multipart cannot parse — an error Starlette does not translate.
    boundary = "brain-test-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + _png()
        + f"\r\n--{boundary}\r\nBad Header: x\r\n\r\nx\r\n--{boundary}--\r\n".encode()
    )
    broken = await client.post(
        SECRETARIA_MESSAGES,
        headers={**headers, "content-type": f"multipart/form-data; boundary={boundary}"},
        content=body,
    )
    assert broken.status_code == 422, broken.text
    assert broken.json()["detail"]["code"] == "attachment_malformed"
    assert calls == []


async def test_any_patient_uploads_even_unverified_within_the_budgets_and_the_kill_switch(
    pclient, monkeypatch
):
    """The owner's rule (2026-09-18): a visitor whose e-mail is NOT verified sends files too —
    as its visit's own handle, at the clinic its link named. What still refuses, before a byte
    of the file is read: each patient's own budget, a budget shared by the UNVERIFIED uploads
    of one clinic (a visit is minted per call, so its own budget alone bounds nothing), and
    the kill switch."""
    from brain_api.api.portal import patient_access as router_mod
    from brain_api.config import get_settings

    client, _, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)

    visit = (await client.post("/patient-access/pending", json={"invite": str(seed.both)})).json()
    image = _png()
    resp = await _upload(client, visit["pending_token"], "pedido.png", image, text="Meu pedido")
    assert resp.status_code == 200, resp.text
    parts = _wire_parts(calls[0])
    assert parts["tenant_id"]["content"] == str(seed.both).encode()
    assert parts["external_id"]["content"] == visit["patient_ref"].encode()  # the visit's handle
    assert parts["text"]["content"] == b"Meu pedido"
    assert parts["file"]["content"] == image

    # The clinic's shared budget for unverified uploads: a SECOND visit — what a script minting
    # visits would get — is refused once it is spent...
    monkeypatch.setattr(router_mod._pending_attachment_limiter, "_limit_getter", lambda: 1)
    client.cookies.clear()  # otherwise `/pending` resumes the same visit
    other = (await client.post("/patient-access/pending", json={"invite": str(seed.both)})).json()
    assert other["patient_ref"] != visit["patient_ref"]
    assert (await _upload(client, other["pending_token"], "a.png", _png())).status_code == 429

    # ...while a verified patient of the same clinic never spends it.
    token = (await _patient(pclient))["access_token"]
    assert (await _upload(client, token, "a.png", _png())).status_code == 200

    # Each patient's own budget, verified or not.
    monkeypatch.setattr(router_mod._attachment_limiter, "_limit_getter", lambda: 1)
    assert (await _upload(client, token, "a.png", _png())).status_code == 429

    monkeypatch.setattr(get_settings(), "PATIENT_ATTACHMENTS_ENABLED", False)
    off = await _upload(client, token, "a.png", _png())
    assert off.status_code == 422
    assert off.json()["detail"]["code"] == "attachment_unsupported_for_product"
    assert len(calls) == 2  # the visitor's upload and the verified patient's first one


# --- 4) The file comes back only inside its own conversation -----------------------------------


async def test_media_is_served_only_inside_the_owners_conversation(pclient, monkeypatch):
    """Checklist 4: the owner gets the bytes; ANOTHER authenticated patient asking for the same
    id gets a 404 — not a 403 — identical to an id that never existed, and the upstream saw
    THAT patient's session, never the owner's: the id alone selects nothing."""
    client, _, seed = pclient
    _configure_mesh(monkeypatch)
    owner = await _patient(pclient)
    other = await _patient(pclient, email="outra.paciente@exemplo.com")
    visitor = (await client.post("/patient-access/pending", json={"invite": str(seed.both)})).json()
    message_id = str(uuid.uuid4())
    image = _png()

    def answer(request):
        params = request.url.params
        owns = (
            params.get("tenant_id") == str(seed.both)
            and params.get("external_id") == owner["patient_ref"]
        )
        if request.url.path == f"/internal/brain-message/media/{message_id}" and owns:
            return httpx.Response(
                200, content=image, headers={"content-type": "image/png"}, request=request
            )
        return httpx.Response(404, json={"detail": "not_found"}, request=request)

    calls = _mesh(monkeypatch, answer)
    path = f"/patient-access/threads/secretaria/media/{message_id}"

    mine = await client.get(path, headers=_bearer(owner["access_token"]))
    assert mine.status_code == 200, mine.text
    assert mine.content == image
    assert mine.headers["content-type"] == "image/png"
    assert mine.headers["content-length"] == str(len(image))
    assert mine.headers["x-content-type-options"] == "nosniff"
    assert mine.headers["cache-control"] == "private, no-store"
    assert mine.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert mine.headers["content-disposition"] == 'inline; filename="anexo.png"'
    assert calls[0]["headers"]["x-internal-api-key"] == "secretaria-key-AAA"

    theirs = await client.get(path, headers=_bearer(other["access_token"]))
    assert theirs.status_code == 404
    assert theirs.json()["detail"]["code"] == "attachment_not_found"
    assert calls[1]["params"] == {"tenant_id": str(seed.both), "external_id": other["patient_ref"]}

    pending = await client.get(path, headers=_bearer(visitor["pending_token"]))
    assert pending.status_code == 404
    assert calls[2]["params"]["external_id"] == visitor["patient_ref"]

    ghost = await client.get(
        f"/patient-access/threads/secretaria/media/{uuid.uuid4()}",
        headers=_bearer(owner["access_token"]),
    )
    assert ghost.status_code == 404
    assert ghost.json() == theirs.json() == pending.json()  # indistinguishable

    assert (await client.get(path)).status_code == 401  # and never without a session


async def test_media_refuses_what_it_must_never_serve(pclient, monkeypatch):
    """A malformed id and a product without files cost no network; an upstream answering a
    type outside the accepted kinds is refused, never streamed from this origin."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=b"<script>alert(1)</script>",
            headers={"content-type": "text/html"},
            request=request,
        ),
    )
    headers = _bearer((await _patient(pclient))["access_token"])

    # (A literal ".." never gets here: the client normalizes it away before the request, and
    # `is_media_id` refuses it anyway — see the unit test at the end of this module.)
    for bad in ("a.b", "x" * 65, "%2E%2E"):
        resp = await client.get(f"/patient-access/threads/secretaria/media/{bad}", headers=headers)
        assert resp.status_code in (404, 422), bad  # 404 only if the client resolved it away
        assert resp.status_code == 422 or bad == "%2E%2E", bad
    precheck = await client.get(
        f"/patient-access/threads/precheck/media/{uuid.uuid4()}", headers=headers
    )
    assert precheck.status_code == 404
    assert calls == []

    html = await client.get(
        f"/patient-access/threads/secretaria/media/{uuid.uuid4()}", headers=headers
    )
    assert html.status_code == 502
    assert "<script>" not in html.text


async def test_the_transcript_carries_a_reference_never_the_storage(pclient, monkeypatch):
    """The poll passes the transcript through, except `attachment`: a whitelist plus the
    route that streams it. An object key or a signed URL never reaches the browser."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    with_file, plain, bad = (str(uuid.uuid4()) for _ in range(3))
    transcript = {
        "data": [
            {
                "id": with_file,
                "body": "[anexo: exame.png]",
                "attachment": {
                    "r2_object_key": "tenants/abc/objects/xyz.png",
                    "content_type": "image/png",
                    "size_bytes": 68,
                    "filename": "../../exame.png",
                    "url": "https://bucket.r2.example/xyz.png?X-Amz-Signature=abc",
                },
            },
            {"id": plain, "body": "Recebido!"},
            {"id": bad, "body": "?", "attachment": {"content_type": "text/html", "size_bytes": 9}},
        ]
    }
    _mesh(monkeypatch, lambda request: httpx.Response(200, json=transcript, request=request))
    login = await _patient(pclient)

    resp = await client.get(SECRETARIA_MESSAGES, headers=_bearer(login["access_token"]))

    assert resp.status_code == 200, resp.text
    data = resp.json()["payload"]["data"]
    assert data[0]["attachment"] == {
        "content_type": "image/png",
        "size_bytes": 68,
        "filename": "exame.png",
        "media_path": f"/patient-access/threads/secretaria/media/{with_file}",
    }
    assert "attachment" not in data[1]
    assert data[2]["attachment"] is None
    assert "r2_object_key" not in resp.text and "X-Amz-Signature" not in resp.text


# --- 5) A message without a file is the same JSON relay as before ------------------------------


async def test_a_message_without_a_file_is_the_same_json_relay_as_before(pclient, monkeypatch):
    """Checklist 5: JSON in, JSON out, same body; and the JSON contract's refusals keep
    FastAPI's exact shape now that the route reads its own body."""
    client, _, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _queued)
    login = await _patient(pclient)
    headers = _bearer(login["access_token"])

    resp = await client.post(SECRETARIA_MESSAGES, headers=headers, json={"text": "Bom dia"})
    assert resp.status_code == 200, resp.text
    assert calls[0]["headers"]["content-type"] == "application/json"
    assert json.loads(calls[0]["body"]) == {
        "tenant_id": str(seed.both),
        "external_id": login["patient_ref"],
        "text": "Bom dia",
    }

    empty = await client.post(SECRETARIA_MESSAGES, headers=headers, json={"text": ""})
    assert empty.status_code == 422
    assert empty.json()["detail"][0]["loc"] == ["body", "text"]
    aimed = await client.post(
        SECRETARIA_MESSAGES, headers=headers, json={"text": "oi", "tenant_id": str(uuid.uuid4())}
    )
    assert aimed.status_code == 422
    assert aimed.json()["detail"][0]["type"] == "extra_forbidden"
    broken = await client.post(
        SECRETARIA_MESSAGES,
        headers={**headers, "content-type": "application/json"},
        content=b'{"text": ',
    )
    assert broken.status_code == 422
    assert broken.json()["detail"][0]["type"] == "json_invalid"
    plain_text = await client.post(
        SECRETARIA_MESSAGES, headers={**headers, "content-type": "text/plain"}, content=b"oi"
    )
    assert plain_text.status_code == 422
    missing = await client.post(SECRETARIA_MESSAGES, headers=headers)
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"] == ["body"]
    as_json = {**headers, "content-type": "application/json"}
    null = await client.post(SECRETARIA_MESSAGES, headers=as_json, content=b"null")
    assert null.status_code == 422
    assert null.json()["detail"][0]["type"] == "missing"  # FastAPI reads `null` as no body
    array = await client.post(SECRETARIA_MESSAGES, headers=as_json, content=b"[1, 2]")
    assert array.status_code == 422
    assert array.json()["detail"][0]["type"] == "model_attributes_type"
    assert array.json()["detail"][0]["input"] == [1, 2]
    not_unicode = await client.post(SECRETARIA_MESSAGES, headers=as_json, content=b"\xff\xfe\xfa")
    assert not_unicode.status_code == 400  # FastAPI's own answer, never a 500
    huge_number = await client.post(SECRETARIA_MESSAGES, headers=as_json, content=b"1" * 5000)
    assert huge_number.status_code == 400  # past Python's integer digit limit
    too_deep = await client.post(SECRETARIA_MESSAGES, headers=as_json, content=b"[" * 60000)
    assert too_deep.status_code == 400  # past the recursion limit
    assert len(calls) == 1


# --- 6) No log carries the name or the bytes --------------------------------------------------


async def test_no_log_line_carries_the_file_name_or_its_bytes(pclient, monkeypatch, caplog, capsys):
    """Checklist 6: relayed, refused and streamed — each leaves its log line, and none of them
    carries the file's name (PII here) or a byte of it."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    secret = b"SEGREDO-CLINICO-7731"
    image = _png(secret)
    message_id = str(uuid.uuid4())

    def answer(request):
        if request.url.path.startswith("/internal/brain-message/media/"):
            return httpx.Response(
                200, content=image, headers={"content-type": "image/png"}, request=request
            )
        return _queued(request)

    _mesh(monkeypatch, answer)
    token = (await _patient(pclient))["access_token"]
    capsys.readouterr()  # only the attachment traffic is under test

    with caplog.at_level(logging.INFO):
        sent = await _upload(client, token, "exame_maria_da_silva_cpf_12345678900.png", image)
        refused = await _upload(client, token, "laudo_maria_da_silva.jpg", b"texto " + secret)
        fetched = await client.get(
            f"/patient-access/threads/secretaria/media/{message_id}", headers=_bearer(token)
        )

    assert (sent.status_code, refused.status_code, fetched.status_code) == (200, 415, 200)
    everything = capsys.readouterr().out + "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("maria_da_silva", "12345678900", secret.decode()):
        assert needle not in everything, f"{needle!r} leaked into a log line"
    for event in (
        "patient_attachment_relayed",
        "patient_attachment_refused",
        "patient_media_streamed",
    ):
        assert event in everything


# --- 7) What only the product can refuse reaches the patient as a 4xx, never a 502 -------------
#
# Production, 2026-09-18/19: a visitor's first file hit secretarIA's LGPD gate (409
# attachment_consent_required), this edge flattened it to `502 product_error`, and EasyPanel's
# gateway swapped that 502 for its own "Service is not reachable" page — read as brain-api
# crashing (docs/CHECKPOINT_brain_message_anexos.md §10).

_UPSTREAM_SENTENCE = "texto interno da secretaria que nunca chega ao navegador"


def _refusing(status_code, detail):
    def answer(request):
        return httpx.Response(status_code, json={"detail": detail}, request=request)

    return answer


@pytest.mark.parametrize(
    ("code", "status_code"),
    [("attachment_consent_required", 409), ("attachment_quota_exceeded", 429)],
)
async def test_a_refusal_only_the_product_can_make_reaches_the_patient_as_its_own_4xx(
    pclient, monkeypatch, capsys, code, status_code
):
    """Consent (in THIS conversation) and the persisted daily quota are the product's facts.
    They reach the patient as the permanent 4xx they are, in this service's own words — the
    product's body still never does."""
    client, _, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(
        monkeypatch, _refusing(status_code, {"code": code, "message": _UPSTREAM_SENTENCE})
    )
    visit = (await client.post("/patient-access/pending", json={"invite": str(seed.both)})).json()
    capsys.readouterr()

    resp = await _upload(client, visit["pending_token"], "exame.png", _png())

    assert resp.status_code == status_code, resp.text
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {"detail": {"code": code, "message": attachments.REFUSALS[code][1]}}
    assert _UPSTREAM_SENTENCE not in resp.text
    assert len(calls) == 1
    logged = capsys.readouterr().out
    assert "switchboard_upstream_refused" in logged
    assert "patient_attachment_refused" in logged
    assert "switchboard_upstream_error" not in logged


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (409, {"detail": {"code": "some_future_refusal", "message": _UPSTREAM_SENTENCE}}),
        # A known code at a status that disagrees with the table is drift, not a refusal.
        (422, {"detail": {"code": "attachment_consent_required", "message": "x"}}),
        # A refusal this edge makes itself, answered by the product after this edge accepted
        # the file: the two repos disagree — a bug to see in the log, not the patient's fault.
        (415, {"detail": {"code": "attachment_type_unsupported", "message": "x"}}),
        (409, {"detail": "attachment_consent_required"}),
        (409, None),
    ],
)
async def test_any_other_refusal_of_an_accepted_file_stays_the_opaque_product_error(
    pclient, monkeypatch, status_code, body
):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)

    def answer(request):
        if body is None:
            return httpx.Response(status_code, text="<html>nope</html>", request=request)
        return httpx.Response(status_code, json=body, request=request)

    _mesh(monkeypatch, answer)
    token = (await _patient(pclient))["access_token"]

    resp = await _upload(client, token, "exame.png", _png())

    assert resp.status_code == 502, resp.text
    assert resp.json() == {"detail": "product_error"}


async def test_a_text_message_and_a_storage_outage_keep_their_answers(pclient, monkeypatch):
    """Only the FILE relay lets the product's refusals through; a text message refused the
    same way is still the opaque 502. And the one transient refusal — storage unavailable —
    stays the retryable 503 every product outage already is."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    _mesh(
        monkeypatch,
        _refusing(409, {"code": "attachment_consent_required", "message": _UPSTREAM_SENTENCE}),
    )
    token = (await _patient(pclient))["access_token"]
    text = await client.post(SECRETARIA_MESSAGES, headers=_bearer(token), json={"text": "oi"})
    assert text.status_code == 502, text.text
    assert text.json() == {"detail": "product_error"}

    _mesh(
        monkeypatch,
        _refusing(503, {"code": "attachment_storage_unavailable", "message": _UPSTREAM_SENTENCE}),
    )
    outage = await _upload(client, token, "exame.png", _png())
    assert outage.status_code == 503, outage.text
    assert outage.json() == {"detail": "product_temporarily_unavailable"}


# --- The contract module itself ----------------------------------------------------------------


def test_the_content_decides_and_the_name_is_made_safe_and_honest():
    assert attachments.sniff_kind(_png()) is attachments.PNG
    assert attachments.sniff_kind(b"%PDF-1.7\n") is attachments.PDF
    assert attachments.sniff_kind(b"junk%PDF-1.7\n") is None  # a polyglot's late header
    assert attachments.sniff_kind(b"RIFF\x00\x00\x00\x00WAVE") is None  # RIFF, but not WEBP

    safe = attachments.safe_filename
    assert safe("../../etc/passwd", attachments.PNG) == "passwd.png"
    assert safe("C:\\Users\\ana\\Foto.JPG", attachments.JPEG) == "Foto.jpg"
    assert safe("nota\u202egnp.exe", attachments.PNG) == "notagnp.png"  # bidi mark dropped
    assert safe(".htaccess", attachments.PNG) == "htaccess.png"
    # Invisible marks go by Unicode CATEGORY: soft hyphen, a tag character, word joiner and
    # the Arabic letter mark — built with chr() so no escape can land on disk as the mark.
    hidden = "re" + chr(0xAD) + "cei" + chr(0xE0041) + "ta" + chr(0x2060) + chr(0x61C) + ".pdf"
    assert safe(hidden, attachments.PDF) == "receita.pdf"
    assert safe("", attachments.PDF) == "anexo.pdf"
    assert safe("Exame 12.03.2026", attachments.PDF) == "Exame 12.03.2026.pdf"
    assert len(safe("a" * 500 + ".png", attachments.PNG)) == attachments.MAX_FILENAME_CHARS

    assert not attachments.extension_conflicts("print.jpg", attachments.PNG)  # same family
    assert attachments.extension_conflicts("exame.jpg", attachments.PDF)  # across families
    assert attachments.extension_conflicts("nota\u202egnp.exe", attachments.PNG)

    assert attachments.is_media_id(str(uuid.uuid4()))
    for bad in ("..", "../x", "a.b", "a/b", "", "-x", "x" * 65):
        assert not attachments.is_media_id(bad), bad
