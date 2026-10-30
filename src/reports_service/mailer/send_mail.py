"""Report delivery via the internal mail-relay HTTP API — replaces the
previous aiosmtplib + local Mailhog sink. No retry/ambiguous-ack handling:
a single request either succeeds or the run is marked "failed" (see
mailer/pipeline.py) and can be re-triggered via the manual `/run` route.

Note: the relay only accepts a subject/recipients/body — it has no
attachment support, so report emails carry the HTML summary only. The CSV
is still available via the dashboard's CSV export route
(reports_service/export/router.py), which calls the same build_csv().
"""

import logging
import re

import httpx

from reports_service.core.config import get_settings

logger = logging.getLogger(__name__)

_HTML_TAG_PATTERN = re.compile(r"<\w+>|</\w+>")


async def send_mail(*, to_addrs: list[str], subject: str, message: str, is_html: bool = False) -> None:
    settings = get_settings()
    if not settings.alert_api_url:
        raise RuntimeError("ALERT_API_URL is not configured")

    payload = {
        "subject": subject,
        "to_list": ",".join(to_addrs),
        "msg_body": message,
    }
    if is_html or _HTML_TAG_PATTERN.search(message):
        payload["is_html"] = "yes"

    async with httpx.AsyncClient(timeout=settings.alert_api_timeout_seconds) as client:
        response = await client.request("GET", settings.alert_api_url, data=payload)
    response.raise_for_status()
    logger.info("Response from mail-relay ::: %s", response)
