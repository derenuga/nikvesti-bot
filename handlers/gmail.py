import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD")

def get_unread_emails():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASSWORD)
    mail.select("inbox", readonly=True)

    _, messages = mail.search(None, "UNSEEN")
    email_ids = messages[0].split()

    if not email_ids:
        mail.logout()
        return []

    emails = []
    for eid in email_ids:
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        subject = decode_header(msg["Subject"])[0]
        subject = subject[0].decode(subject[1] or "utf-8") if isinstance(subject[0], bytes) else subject[0]

        sender = msg.get("From", "")
        date_str = msg.get("Date", "")

        try:
            date = email.utils.parsedate_to_datetime(date_str)
            # parsedate_to_datetime іноді повертає naive datetime, коли заголовок
            # Date: без tz — тоді порівняння з now(tz=UTC) падає (TypeError:
            # can't compare offset-naive and offset-aware datetimes)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except Exception:
            date = datetime.now(timezone.utc)

        emails.append({
            "sender": sender,
            "subject": subject,
            "date": date
        })

    mail.logout()
    return emails

def get_oldest_unread_hours(emails):
    if not emails:
        return 0
    oldest = min(e["date"] for e in emails)
    now = datetime.now(timezone.utc)
    diff = now - oldest
    return int(diff.total_seconds() / 3600)
