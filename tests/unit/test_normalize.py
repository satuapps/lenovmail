# Lenovmail — authored by satuapps
"""Tests for MIME normalization: body, attachments, dedup, sanitization, and broken headers."""

from __future__ import annotations

from email.message import EmailMessage

from lenovmail.sync.normalize import (
    html_to_text,
    normalize_subject,
    parse_message,
    search_tsvector_input,
)


def build_message(
    *,
    subject: str = "Hello",
    body: str = "message body",
    message_id: str | None = "<abc@example.com>",
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Budi Santoso <Budi@Example.COM>"
    msg["To"] = "Siti <siti@example.org>, lain@example.net"
    msg["Cc"] = "cc@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 21 Sep 2026 10:00:00 +0700"
    if message_id:
        msg["Message-ID"] = message_id
    for key, value in (extra_headers or {}).items():
        msg[key] = value

    if html is not None:
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body)

    for filename, payload, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


class TestBasicParsing:
    def test_headers_addresses_and_body(self):
        parsed = parse_message(build_message(body="payment overdue this month"))

        assert parsed.subject == "Hello"
        assert parsed.from_addr == "budi@example.com"  # normalized to lowercase
        assert parsed.from_name == "Budi Santoso"
        assert [a["addr"] for a in parsed.to_addrs] == ["siti@example.org", "lain@example.net"]
        assert [a["addr"] for a in parsed.cc_addrs] == ["cc@example.org"]
        assert parsed.body_text is not None
        assert "payment overdue" in parsed.body_text
        assert parsed.snippet == "payment overdue this month"
        assert parsed.sent_date is not None and parsed.sent_date.year == 2026
        assert parsed.size_bytes == len(build_message(body="payment overdue this month"))
        assert parsed.headers["message-id"] == "<abc@example.com>"

    def test_dedup_hash_is_stable_per_message_id(self):
        first = parse_message(build_message(message_id="<same@example.com>", body="a"))
        second = parse_message(build_message(message_id="<same@example.com>", body="b"))

        assert first.dedup_hash == second.dedup_hash

    def test_dedup_hash_differs_for_different_message_ids(self):
        first = parse_message(build_message(message_id="<one@example.com>"))
        second = parse_message(build_message(message_id="<two@example.com>"))

        assert first.dedup_hash != second.dedup_hash

    def test_missing_message_id_falls_back_to_raw_hash(self):
        raw = build_message(message_id=None)
        parsed = parse_message(raw)

        assert parsed.rfc822_message_id is None
        assert parsed.dedup_hash is not None
        # Identical bytes -> identical hash; different bytes -> different hash.
        assert parsed.dedup_hash == parse_message(raw).dedup_hash
        assert parsed.dedup_hash != parse_message(raw + b"\r\n").dedup_hash

    def test_search_input_includes_subject_and_body(self):
        parsed = parse_message(build_message(subject="Invoice ACME 42", body="payment"))
        text = search_tsvector_input(
            parsed.subject, parsed.from_name, parsed.from_addr, parsed.body_text
        )

        assert "Invoice ACME 42" in text and "payment" in text and "budi@example.com" in text


class TestAttachments:
    def test_multipart_mixed_attachment_numbering(self):
        raw = build_message(
            body="see attachment",
            attachments=[("report.pdf", b"%PDF-1.4 dummy", "application/pdf")],
        )
        parsed = parse_message(raw)

        assert parsed.has_attachments is True
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.filename == "report.pdf"
        assert attachment.mime_type == "application/pdf"
        assert attachment.size_bytes == len(b"%PDF-1.4 dummy")
        assert attachment.is_inline is False
        # multipart/mixed -> IMAP part number
        assert attachment.part_path == "2"

    def test_alternative_with_attachment_gets_nested_part_path(self):
        raw = build_message(
            body="text",
            html="<p>html</p>",
            attachments=[("note.txt", b"note", "text/plain")],
        )
        parsed = parse_message(raw)

        # multipart/mixed( multipart/alternative(1.1, 1.2), attachment(2) )
        assert [a.part_path for a in parsed.attachments] == ["2"]
        assert parsed.body_text is not None and "text" in parsed.body_text
        assert parsed.body_html is not None and "<p>html</p>" in parsed.body_html

    def test_inline_image_is_flagged_inline(self):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["Subject"] = "inline"
        msg.set_content("see image")
        msg.add_alternative('<p>see <img src="cid:logo1"></p>', subtype="html")
        html_part = msg.get_payload()[1]
        html_part.add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<logo1>")
        parsed = parse_message(msg.as_bytes())

        images = [a for a in parsed.attachments if a.mime_type == "image/png"]
        assert images and images[0].is_inline is True
        assert images[0].content_id == "logo1"


class TestHtmlSanitisation:
    def test_script_and_event_handlers_removed(self):
        raw = build_message(
            body="fallback",
            html='<p onclick="evil()">safe</p><script>alert(1)</script>'
            '<a href="javascript:evil()">click</a>',
        )
        parsed = parse_message(raw)

        assert parsed.body_html is not None
        assert "<script" not in parsed.body_html.lower()
        assert "onclick" not in parsed.body_html.lower()
        assert "javascript:" not in parsed.body_html.lower()
        assert "safe" in parsed.body_html

    def test_html_to_text_breaks_blocks_and_unescapes(self):
        text = html_to_text("<p>line one</p><p>line &amp; two<br>three</p>")

        assert "line one" in text
        assert "line & two" in text


class TestBrokenInput:
    def test_html_only_message_gets_text_fallback(self):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["Subject"] = "html only"
        msg.set_content("<p>html only</p>", subtype="html")
        parsed = parse_message(msg.as_bytes())

        assert parsed.body_html is not None
        assert parsed.body_text is not None and "html only" in parsed.body_text

    def test_unparsable_date_is_none_not_error(self):
        raw = (
            b"From: a@example.com\r\n"
            b"Subject: broken date\r\n"
            b"Date: not a date at all\r\n"
            b"Message-ID: <baddate@example.com>\r\n\r\n"
            b"body\r\n"
        )
        parsed = parse_message(raw)

        assert parsed.sent_date is None
        assert parsed.subject == "broken date"

    def test_unknown_charset_still_decodes_body(self):
        raw = (
            b"From: a@example.com\r\n"
            b"Subject: weird charset\r\n"
            b"Message-ID: <cs@example.com>\r\n"
            b"Content-Type: text/plain; charset=x-unknown\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n\r\n"
            b"body still decodes\r\n"
        )
        parsed = parse_message(raw)

        assert parsed.body_text is not None
        assert "body still decodes" in parsed.body_text

    def test_rfc2047_encoded_subject_is_decoded(self):
        raw = (
            b"From: a@example.com\r\n"
            b"Subject: =?utf-8?B?SW52b2ljZSBBQ01FIOKckw==?=\r\n"
            b"Message-ID: <enc@example.com>\r\n\r\n"
            b"body\r\n"
        )
        parsed = parse_message(raw)

        assert parsed.subject is not None
        assert "Invoice ACME" in parsed.subject

    def test_empty_bytes_does_not_raise(self):
        parsed = parse_message(b"")

        assert parsed.subject is None
        assert parsed.body_text is None
        assert parsed.from_addr is None


class TestSubjectNormalisation:
    def test_reply_and_forward_prefixes_are_stripped(self):
        for value in ("Re: Hello", "RE: Hello", "Fwd: Hello", "Re: Fwd: Hello", "Balas: Hello"):
            assert normalize_subject(value) == "hello"

    def test_repeated_and_numbered_prefixes(self):
        assert normalize_subject("Re[2]: Re: Report Q3") == "report q3"

    def test_prefix_without_colon_is_not_stripped(self):
        assert normalize_subject("Report: Q3") == "report: q3"
        assert normalize_subject("Actuals Q3") == "actuals q3"

    def test_empty_and_none(self):
        assert normalize_subject("") is None
        assert normalize_subject(None) is None
        assert normalize_subject("Re:") is None
