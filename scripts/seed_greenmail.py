#!/usr/bin/env python
# Lenovmail — authored by satuapps (satuapps.com)
"""Seed the test IMAP server with synthetic messages (used for at-scale verification).

Example:
    uv run python scripts/seed_greenmail.py --count 2000

Always inserts two marker messages used by verification assertions:
  * subject `Invoice ACME 42`, body containing the phrase `payment overdue`
  * subject `Q3 Report` with attachment `report.pdf`, plus a reply `Re: Q3 Report`
    that references its Message-ID (to test threading)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from imapclient import IMAPClient

MARKER_INVOICE_SUBJECT = "Invoice ACME 42"
MARKER_INVOICE_BODY = "Please process urgently, payment has been overdue since last week."
MARKER_REPORT_SUBJECT = "Q3 Report"
MARKER_REPORT_ID = "<q3-report-marker@lenov.test>"
MARKER_REPLY_SUBJECT = "Re: Q3 Report"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

SENDERS = [
    ("Dana Whitfield", "dana@example.com"),
    ("Marta Reyes", "marta@example.org"),
    ("Ops Monitoring", "alerts@monitoring.example.net"),
    ("Priya Raman", "priya@example.net"),
    ("Billing", "billing@vendor.example.com"),
]
TOPICS = [
    "Weekly meeting",
    "Order confirmation",
    "Deploy notification",
    "Sales report",
    "Support ticket",
    "Daily summary",
    "Review invitation",
]


def build(index: int, base: datetime) -> tuple[bytes, datetime]:
    name, addr = SENDERS[index % len(SENDERS)]
    topic = TOPICS[index % len(TOPICS)]
    when = base - timedelta(minutes=index * 7)
    msg = EmailMessage()
    msg["From"] = f"{name} <{addr}>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = f"{topic} #{index}"
    msg["Message-ID"] = f"<seed-{index}@lenov.test>"
    msg["Date"] = format_datetime(when)
    msg.set_content(
        f"Hello,\n\nThis is test message number {index} on the topic {topic}.\n"
        "No action is required.\n"
    )
    return msg.as_bytes(), when


def invoice_marker(base: datetime) -> tuple[bytes, datetime]:
    msg = EmailMessage()
    msg["From"] = "Billing <billing@acme.example.com>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = MARKER_INVOICE_SUBJECT
    msg["Message-ID"] = "<invoice-acme-42@lenov.test>"
    msg["Date"] = format_datetime(base)
    msg.set_content(MARKER_INVOICE_BODY)
    return msg.as_bytes(), base


def report_marker(base: datetime) -> tuple[bytes, datetime]:
    when = base - timedelta(minutes=5)
    msg = EmailMessage()
    msg["From"] = "Nadia Larsen <nadia@example.net>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = MARKER_REPORT_SUBJECT
    msg["Message-ID"] = MARKER_REPORT_ID
    msg["Date"] = format_datetime(when)
    msg.set_content("Attached is the Q3 report.")
    msg.add_attachment(
        PDF_BYTES, maintype="application", subtype="pdf", filename="report.pdf"
    )
    return msg.as_bytes(), when


def report_reply(base: datetime) -> tuple[bytes, datetime]:
    when = base - timedelta(minutes=1)
    msg = EmailMessage()
    msg["From"] = "Dana Whitfield <dana@example.com>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = MARKER_REPLY_SUBJECT
    msg["Message-ID"] = "<q3-report-reply@lenov.test>"
    msg["In-Reply-To"] = MARKER_REPORT_ID
    msg["References"] = MARKER_REPORT_ID
    msg["Date"] = format_datetime(when)
    msg.set_content("Thanks, I'll take a look.")
    return msg.as_bytes(), when


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3143)
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password", default="demo")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--count", type=int, default=2000, help="number of regular messages")
    parser.add_argument(
        "--subject", default=None, help="if set, send only a single message with this subject"
    )
    parser.add_argument("--ssl", action="store_true", help="use IMAPS")
    args = parser.parse_args()

    base = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    client = IMAPClient(args.host, args.port, ssl=args.ssl)
    client.login(args.user, args.password)
    try:
        if args.subject:
            started = time.monotonic()
            now = datetime.now(UTC)
            msg = EmailMessage()
            msg["From"] = "Dana Whitfield <dana@example.com>"
            msg["To"] = "demo@lenov.test"
            msg["Subject"] = args.subject
            msg["Message-ID"] = f"<manual-{int(time.time())}@lenov.test>"
            msg["Date"] = format_datetime(now)
            msg.set_content(f"Hello,\n\nSpecial message: {args.subject}\n")
            client.append(args.mailbox, msg.as_bytes(), flags=(b"\\Recent",), msg_time=now)
            print(f"1 message '{args.subject}' added in {time.monotonic() - started:.2f}s")
            return 0

        started = time.monotonic()
        for index in range(1, args.count + 1):
            raw, when = build(index, base)
            client.append(args.mailbox, raw, msg_time=when)
            if index % 250 == 0:
                elapsed = time.monotonic() - started
                print(f"  {index}/{args.count} messages ({elapsed:.1f}s)", file=sys.stderr)
        # Verification markers.
        for raw, when in (invoice_marker(base), report_marker(base), report_reply(base)):
            client.append(args.mailbox, raw, msg_time=when)

        total = args.count + 3
        print(f"{total} messages added to {args.mailbox} in {time.monotonic() - started:.1f}s")
    finally:
        client.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
