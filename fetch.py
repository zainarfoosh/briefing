"""Pull the day's biopharma newsletters out of Gmail over IMAP.

Gmail labels show up as IMAP folders, but the exact name depends on whether the
label is nested. Run `python fetch.py --list-folders` once against the real
account and confirm the string before wiring up anything downstream.
"""

from __future__ import annotations

import argparse
import email
import email.utils
import imaplib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message

from bs4 import BeautifulSoup

IMAP_HOST = "imap.gmail.com"
# Case-sensitive: this is the Gmail label spelled exactly as it appears in the UI.
DEFAULT_FOLDER = "BioPharma"

# Bounds token cost. Newsletters bury the substance in the first few thousand
# characters and spend the rest on footers.
MAX_CHARS_PER_EMAIL = 8000
MAX_LINKS_PER_EMAIL = 40

# Anchor text that is navigation, not a story.
BOILERPLATE_LINK = re.compile(
    r"unsubscribe|view (this )?(email )?in( your)? browser|privacy policy"
    r"|terms of (use|service)|manage (your )?(email )?preferences"
    r"|update your (profile|preferences)|forward to a friend|add us to your address"
    r"|advertise|sponsored by|follow us|contact us|read online"
    r"|^\s*(twitter|linkedin|facebook|instagram|x|share|here)\s*$",
    re.I,
)


@dataclass
class Email:
    """One newsletter, cleaned up and ready to hand to the summarizer."""

    message_id: str
    sender: str
    subject: str
    date: datetime
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def connect() -> imaplib.IMAP4_SSL:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        sys.exit("GMAIL_USER and GMAIL_APP_PASSWORD must be set in the environment.")

    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as exc:
        sys.exit(
            f"Gmail rejected the login: {exc}\n"
            "If this is an app password, confirm 2-Step Verification is on and the "
            "password was pasted without spaces."
        )
    return conn


def list_folders(conn: imaplib.IMAP4_SSL) -> None:
    """Print every IMAP folder so the Biopharma label's real name is visible."""
    status, folders = conn.list()
    if status != "OK":
        sys.exit("Could not list folders.")
    for raw in folders:
        print(raw.decode("utf-8", errors="replace"))


def _html_to_text_and_links(html: str) -> tuple[str, list[tuple[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "head", "title", "meta"]):
        tag.decompose()

    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        label = " ".join(anchor.get_text(" ", strip=True).split())
        if not href.startswith("http") or href in seen_urls:
            continue
        if not label or BOILERPLATE_LINK.search(label):
            continue
        seen_urls.add(href)
        links.append((label[:200], href))
        if len(links) >= MAX_LINKS_PER_EMAIL:
            break

    text = soup.get_text("\n")
    return text, links


def _clean_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    collapsed = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def _extract_body(msg: Message) -> tuple[str, list[tuple[str, str]]]:
    """Prefer text/plain; fall back to stripped HTML. Links come from the HTML."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")

        if part.get_content_type() == "text/plain":
            plain_parts.append(decoded)
        elif part.get_content_type() == "text/html":
            html_parts.append(decoded)

    html = "\n".join(html_parts)
    links: list[tuple[str, str]] = []
    html_text = ""
    if html:
        html_text, links = _html_to_text_and_links(html)

    text = _clean_text("\n".join(plain_parts)) or _clean_text(html_text)
    return text[:MAX_CHARS_PER_EMAIL], links


def fetch(
    folder: str,
    cutoff: datetime,
    seen_ids: set[str],
    limit: int | None = None,
) -> list[Email]:
    """Return newsletters newer than `cutoff` whose Message-ID we haven't seen."""
    conn = connect()
    try:
        status, _ = conn.select(f'"{folder}"', readonly=True)
        if status != "OK":
            sys.exit(
                f'Could not open folder "{folder}". '
                "Run --list-folders to see the exact name Gmail uses for the label."
            )

        # IMAP SINCE has day granularity, so search one day wide and filter on the
        # Date header afterwards.
        since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", since)
        if status != "OK":
            sys.exit("IMAP search failed.")

        nums = data[0].split()
        if limit:
            nums = nums[-limit:]

        results: list[Email] = []
        for num in nums:
            status, payload = conn.fetch(num, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue

            msg = email.message_from_bytes(payload[0][1])

            message_id = (msg.get("Message-ID") or "").strip()
            if not message_id or message_id in seen_ids:
                continue

            date = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            if date is None:
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            if date < cutoff:
                continue

            text, links = _extract_body(msg)
            if not text:
                continue

            results.append(
                Email(
                    message_id=message_id,
                    sender=_decode_header(msg.get("From")),
                    subject=_decode_header(msg.get("Subject")),
                    date=date,
                    text=text,
                    links=links,
                )
            )

        results.sort(key=lambda e: e.date)
        return results
    finally:
        try:
            conn.close()
        except imaplib.IMAP4.error:
            pass
        conn.logout()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-folders", action="store_true")
    # `or` not `get(default)`: an unset GitHub Actions variable arrives as "".
    parser.add_argument("--folder", default=os.environ.get("GMAIL_FOLDER") or DEFAULT_FOLDER)
    parser.add_argument("--hours", type=int, default=26, help="Look back this many hours.")
    parser.add_argument("--limit", type=int, help="Only inspect the newest N messages.")
    args = parser.parse_args()

    if args.list_folders:
        conn = connect()
        try:
            list_folders(conn)
        finally:
            conn.logout()
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    emails = fetch(args.folder, cutoff, seen_ids=set(), limit=args.limit)

    print(f"{len(emails)} email(s) since {cutoff:%Y-%m-%d %H:%M} UTC\n")
    for item in emails:
        print(f"  {item.date:%m-%d %H:%M}  {item.sender}")
        print(f"    {item.subject}")
        print(f"    {len(item.text)} chars, {len(item.links)} links")


if __name__ == "__main__":
    main()
