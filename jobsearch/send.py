"""Send the digest from the owner's Gmail to itself via SMTP + app password (stored in DATA_DIR/.env)."""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)
REQUIRED_ENV = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    for key in REQUIRED_ENV:  # name the key and file only, never a value
        if not env.get(key):
            raise RuntimeError(f"missing {key} in {path}")
    return env


def build_message(subject: str, html_body: str, text_body: str, env: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env["GMAIL_ADDRESS"]
    msg["To"] = env.get("DIGEST_TO") or env["GMAIL_ADDRESS"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _default_factory(host: str, port: int, timeout: float) -> smtplib.SMTP_SSL:
    """Implicit TLS with full certificate + hostname verification (looked up at call time for tests)."""
    return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())


def send_email(subject: str, html_body: str, text_body: str, env: dict,
               smtp_factory=None, sleep=time.sleep, delays=(5, 30, 120)) -> None:
    smtp_factory = smtp_factory or _default_factory
    msg = build_message(subject, html_body, text_body, env)
    last: Exception | None = None
    for delay in (0, *delays):
        if delay:
            sleep(delay)
        delivered = False
        try:
            with smtp_factory("smtp.gmail.com", 465, timeout=60) as s:
                s.login(env["GMAIL_ADDRESS"], env["GMAIL_APP_PASSWORD"])
                s.send_message(msg)
                delivered = True
            return
        except (smtplib.SMTPException, OSError) as e:
            if delivered:  # e.g. QUIT failed after the server accepted the message: never send twice
                log.warning("error after delivery ignored: %s: %s", type(e).__name__, e)
                return
            if isinstance(e, smtplib.SMTPRecipientsRefused):  # permanent: retrying cannot help
                raise RuntimeError(f"send rejected by Gmail (SMTPRecipientsRefused): {e.recipients}") from e
            if isinstance(e, (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused)) or (
                    isinstance(e, smtplib.SMTPResponseException) and e.smtp_code >= 500):
                detail = e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else e.smtp_error
                raise RuntimeError(f"send rejected by Gmail ({type(e).__name__} {e.smtp_code}): {detail}") from e
            last = e
    raise RuntimeError(f"send failed after retries: {last}")
