"""Outbound authentication e-mail — brain-api's own SMTP sender.

Until TASK-056 every e-mail of this service went through secretarIA
(`POST /internal/notifications/email`). Authentication mail is the identity authority's
job, so the four auth templates live HERE now; secretarIA keeps only its own product mail
(`connection_success` still goes through `secretaria_provisioning`).

Contract: `send_email` NEVER raises and returns whether the message was handed to the SMTP
server. Rendering is STRICT — a missing variable is a code defect and refuses the send,
instead of mailing a literal `{placeholder}`. Logs carry the template id and the outcome
only: never the recipient, the body, a code, a password or a link.

Same transport as secretarIA's proven sender (stdlib `smtplib` in a worker thread,
STARTTLS, 15 s timeout), so a slow mail server never blocks the event loop.
"""

import asyncio
import smtplib
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage

from brain_api.config import get_settings
from brain_api.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class EmailTemplate:
    subject: str
    body: str


TEMPLATES: dict[str, EmailTemplate] = {
    # Staff password reset — api/auth.py::password_reset_request.
    "password_reset_code": EmailTemplate(
        subject="Seu código para redefinir a senha — Brain",
        body=(
            "Olá, {name}!\n\n"
            "Recebemos um pedido para redefinir a senha da sua conta Brain.\n\n"
            "Seu código é:\n\n"
            "{code}\n\n"
            "Ele expira em {ttl_minutes} minutos e só pode ser usado uma vez. "
            "Não compartilhe este código com ninguém.\n\n"
            "Se você não pediu a redefinição, ignore este e-mail — sua senha continua a mesma.\n\n"
            "— Equipe Brain"
        ),
    ),
    # Team first access — services/team.py::deliver_first_access.
    "staff_temporary_password": EmailTemplate(
        subject="Bem-vindo(a) à equipe da {clinic_name} — Brain",
        body=(
            "Olá, {name}!\n\nBem-vindo(a) à equipe da {clinic_name}.\n\n"
            "Seu e-mail de acesso: {email}\nSua senha temporária: {temporary_password}\n\n"
            "Acesse o portal: {login_url}\n\n"
            "No primeiro acesso, você deverá criar sua senha pessoal antes de usar o portal. "
            "A senha temporária expira em 72 horas. Não compartilhe sua senha.\n\n— Equipe Brain"
        ),
    ),
    # Professional / secretary invite — api/onboarding.py.
    "professional_invite": EmailTemplate(
        subject="Você foi convidado(a) para a equipe da {clinic_name} — Brain",
        body=(
            "Olá, {name}!\n\n"
            "Você foi adicionado(a) à equipe da {clinic_name} na Brain.\n\n"
            "Para definir sua senha e acessar o painel, abra o link abaixo:\n"
            "{link}\n\n"
            "Este link é de uso único e expira em 72 horas.\n\n"
            "— Equipe Brain"
        ),
    ),
    # Patient access code — api/portal/patient_access.py and api/portal/internal.py.
    # ONLY code + ttl: the request answers alike whether or not the clinic exists, so
    # naming a clinic here would leak in the inbox what the endpoint refuses to leak.
    "patient_access_otp": EmailTemplate(
        subject="Seu código de acesso — Brain",
        body=(
            "Olá!\n\n"
            "Seu código para entrar na conversa com a clínica é:\n\n"
            "{code}\n\n"
            "Ele expira em {ttl_minutes} minutos e só pode ser usado uma vez.\n\n"
            "Se você não pediu esse código, pode ignorar este e-mail — ninguém entra "
            "na conversa sem ele.\n\n"
            "— Equipe Brain"
        ),
    ),
}


def is_configured() -> bool:
    return bool(get_settings().SMTP_HOST.strip())


def _header(value: str) -> str:
    """One line, no CR/LF: header values carry clinic-supplied text (clinic_name)."""
    return " ".join(str(value).splitlines()).strip()


def render(template: str, variables: Mapping[str, object]) -> tuple[str, str]:
    """(subject, body). Raises KeyError for an unknown template or a missing variable."""
    tpl = TEMPLATES[template]
    values = dict(variables)
    return _header(tpl.subject.format_map(values)), tpl.body.format_map(values)


def _smtp_send(to: str, subject: str, body: str) -> None:
    """Blocking send — always called through `asyncio.to_thread`."""
    settings = get_settings()
    from_addr = settings.EMAIL_FROM_ADDRESS or settings.SMTP_USERNAME
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        f"{settings.EMAIL_FROM_NAME} <{from_addr}>" if settings.EMAIL_FROM_NAME else from_addr
    )
    message["To"] = to
    message.set_content(body)
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_email(to: str, template: str, variables: Mapping[str, object]) -> bool:
    """Render and send one authentication e-mail. NEVER raises."""
    if not is_configured():
        logger.warning("email_not_configured", template=template)
        return False
    if template not in TEMPLATES:
        logger.error("email_unknown_template", template=template)
        return False
    try:
        subject, body = render(template, variables)
    except (KeyError, IndexError, ValueError) as exc:
        logger.error("email_render_failed", template=template, error_type=type(exc).__name__)
        return False
    try:
        await asyncio.to_thread(_smtp_send, _header(to), subject, body)
    except Exception as exc:  # noqa: BLE001 — the contract is "never raises"
        logger.warning("email_send_failed", template=template, error_type=type(exc).__name__)
        return False
    logger.info("email_sent", template=template)
    return True
