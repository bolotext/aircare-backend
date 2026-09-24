"""
Ops alert emails, sent via the internal mail-relay HTTP API (not SMTP
directly). Used to notify the ops distribution list of scheduler events
(e.g. call-audio upload completion).

Config (.env, os.getenv only — never hardcoded)
------------------------------------------------
ALERT_API_URL   Internal mail-relay endpoint. Without it, send_mail logs a
                warning and returns None instead of raising.
ALERT_EMAIL_OPS Comma-separated recipient list for ops alerts. Falls back to
                 a hardcoded default distribution list if unset.
"""

import os
import re
from datetime import datetime

import httpx
from dotenv import load_dotenv
from common.log_utils import setup_logger

logger = setup_logger(__name__)

load_dotenv()  # services/scheduler/.env

ALERT_API_URL = os.getenv("ALERT_API_URL")

_DEFAULT_EMAIL_OPS = [
    "ritesh.sharma@gnani.ai",
    "zinat.khan@gnani.ai",
    "narayan.reddy@gnani.ai",
    "sweekar.bangera@gnani.ai",
]
EMAIL_OPS = (
    os.getenv("ALERT_EMAIL_OPS").split(",")
    if os.getenv("ALERT_EMAIL_OPS")
    else _DEFAULT_EMAIL_OPS
)

_HTML_TAG_PATTERN = re.compile(r"<\w+>|</\w+>")


def send_mail(
    subj=None,
    message=None,
    has_attachment=False,
    attachment_type="",
    attachment_path="",
    is_html=False,
):
    if not ALERT_API_URL:
        logger.warning("ALERT_API_URL is not set — skipping send_mail (subj=%r)", subj)
        return None

    try:
        date_ = datetime.now().strftime("%d-%m-%Y")
        team_email = ",".join(EMAIL_OPS)

        if subj is None:
            subj = f"Werize Call Audio SFTP Upload - {date_}"
        if message is None:
            message = (
                "Hi Team,\n\n"
                f"Call audio files were uploaded to the client SFTP server for Date: {date_}.\n\n"
                "Thanks,\nGnani Team"
            )

        payload = {
            "subject": subj,
            "to_list": team_email,
            "msg_body": message,
        }
        if is_html or _HTML_TAG_PATTERN.search(message):
            payload["is_html"] = "yes"

        response = httpx.request("GET", ALERT_API_URL, data=payload)
        logger.info("Response from mail-relay ::: %s", response)
        return response
    except Exception:
        logger.exception("Exception in send_mail")
        return "Error in the Send-Mail-Function"
