"""SMTP delivery with bounded retries and explicit ambiguous-acknowledgement
handling.

An SMTP timeout that happens *during or right after the DATA command* means
the message may or may not have reached the server — retrying blindly risks
sending a duplicate customer-facing email, which is worse than one report
needing a manual check. That case is raised as `AmbiguousSendError` and the
caller (mailer/pipeline.py) marks the run "ambiguous" and stops, with no
further automatic attempts. Every other transport failure (connection
refused, EHLO/AUTH timeout, disconnect before any data was sent) is a clean
retryable failure — `TransientSendError` — and gets bounded retries with
backoff before the run is marked "failed".

`_smtp_send_once` is the single seam tests monkeypatch to simulate each
outcome deterministically, without needing a real SMTP server or reaching
into aiosmtplib's internals.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from email.message import EmailMessage

import aiosmtplib

from reports_service.core.config import get_settings

logger = logging.getLogger(__name__)


class TransientSendError(Exception):
    """A retryable SMTP failure — nothing was necessarily sent yet."""


class AmbiguousSendError(Exception):
    """DATA was sent (or may have been) but no clear ACK arrived before
    timeout. Must never be retried automatically.
    """


@dataclass
class SendOutcome:
    status: str  # "success" | "failed" | "ambiguous"
    attempts: int
    errors: list[str] = field(default_factory=list)


def _build_message(*, to_addrs: list[str], subject: str, html_body: str, csv_bytes: bytes, csv_filename: str) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["From"] = settings.smtp_from_address
    message["To"] = ", ".join(to_addrs)
    message["Subject"] = subject
    message.set_content("This report requires an HTML-capable mail client.")
    message.add_alternative(html_body, subtype="html")
    message.add_attachment(csv_bytes, maintype="text", subtype="csv", filename=csv_filename)
    return message


async def _smtp_send_once(message: EmailMessage) -> None:
    settings = get_settings()
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            timeout=settings.smtp_timeout_seconds,
        )
    except aiosmtplib.SMTPTimeoutError as exc:
        # aiosmtplib's timeout messages name the command in flight; a timeout
        # during/after DATA is the genuinely ambiguous case (the server may
        # have already accepted the message), everything earlier (connect,
        # EHLO, AUTH) is a clean "nothing was sent" retryable failure.
        if "DATA" in str(exc).upper():
            raise AmbiguousSendError(str(exc)) from exc
        raise TransientSendError(str(exc)) from exc
    except (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPServerDisconnected, OSError) as exc:
        raise TransientSendError(str(exc)) from exc


async def send_report_email(
    *, to_addrs: list[str], subject: str, html_body: str, csv_bytes: bytes, csv_filename: str
) -> SendOutcome:
    settings = get_settings()
    message = _build_message(
        to_addrs=to_addrs, subject=subject, html_body=html_body, csv_bytes=csv_bytes, csv_filename=csv_filename
    )

    attempts = 0
    errors: list[str] = []
    while attempts < settings.max_smtp_attempts:
        attempts += 1
        try:
            await _smtp_send_once(message)
            return SendOutcome(status="success", attempts=attempts, errors=errors)
        except AmbiguousSendError as exc:
            errors.append(f"attempt {attempts} (ambiguous): {exc}")
            logger.warning("SMTP send outcome ambiguous on attempt %s — not retrying: %s", attempts, exc)
            return SendOutcome(status="ambiguous", attempts=attempts, errors=errors)
        except TransientSendError as exc:
            errors.append(f"attempt {attempts}: {exc}")
            logger.warning("SMTP send attempt %s failed, will retry if attempts remain: %s", attempts, exc)
            if attempts < settings.max_smtp_attempts:
                await asyncio.sleep(min(2**attempts, 10))

    return SendOutcome(status="failed", attempts=attempts, errors=errors)
