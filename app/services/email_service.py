import os
import aiosmtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")


def _validate_email_config():
    missing = [k for k, v in {
        "SMTP_HOST": SMTP_HOST,
        "SMTP_PORT": SMTP_PORT,
        "SMTP_USERNAME": SMTP_USERNAME,
        "SMTP_PASSWORD": SMTP_PASSWORD,
        "FROM_EMAIL": FROM_EMAIL,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing email config keys in .env: {', '.join(missing)}")


def _extract_refused(refused_like):
    """
    aiosmtplib versions differ:
      - sometimes returns dict of refused recipients
      - sometimes returns tuple where one element is that dict
      - sometimes returns (response, refused_dict) or (refused_dict, response)
    This extracts a dict robustly.
    """
    if refused_like is None:
        return {}
    if isinstance(refused_like, dict):
        return refused_like
    if isinstance(refused_like, tuple):
        for item in refused_like:
            if isinstance(item, dict):
                return item
        return {}
    return {}


async def send_email(to_email: str, subject: str, text_body: str, html_body: str):
    _validate_email_config()

    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")


    smtp = aiosmtplib.SMTP(hostname=SMTP_HOST, port=SMTP_PORT, start_tls=True)
    await smtp.connect()
    await smtp.login(SMTP_USERNAME, SMTP_PASSWORD)

    resp = await smtp.send_message(msg)
    await smtp.quit()

    refused = _extract_refused(resp)

    if refused:
        # refused: { 'bad@domain.com': (code, message) } OR sometimes message only
        first_rcpt, err = next(iter(refused.items()))
        raise RuntimeError(f"Recipient refused: {first_rcpt} {err}")