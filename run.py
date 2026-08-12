"""Fetch, summarize, and write the day's Alexa Flash Briefing feed.

    python run.py --dry-run    # print the script, write nothing
    python run.py              # write feed.json, digest.md, state.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch
import summarize

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
FEED_PATH = ROOT / "feed.json"
DIGEST_PATH = ROOT / "digest.md"

# Enough history to survive a few reruns without re-covering old mail.
MAX_SEEN_IDS = 500

# Alexa ignores feed items beyond the first 5, and any item older than 7 days.
MAX_FEED_ITEMS = 5
FEED_ITEM_MAX_AGE_DAYS = 7

TITLE_TEXT = "Biopharma Briefing"
DEFAULT_REDIRECT = "https://www.statnews.com/"

QUIET_SCRIPT = (
    "Good morning, Zain. Nothing landed overnight worth your time.\n\n"
    "No readouts, no regulatory decisions, no deals in the newsletters that came in. "
    "Quiet mornings happen, and knowing the day is quiet is worth something too.\n\n"
    "That's your rundown. Have a good one."
)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_run": None, "seen_message_ids": []}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict, emails) -> None:
    seen = state.get("seen_message_ids", []) + [e.message_id for e in emails]
    STATE_PATH.write_text(
        json.dumps(
            {
                "last_run": datetime.now(timezone.utc).isoformat(),
                "seen_message_ids": seen[-MAX_SEEN_IDS:],
            },
            indent=2,
        )
        + "\n"
    )


def cutoff_from(state: dict, hours: int) -> datetime:
    """Resume from the last run, but never reach back further than `hours`."""
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    last_run = state.get("last_run")
    if not last_run:
        return floor
    try:
        return max(floor, datetime.fromisoformat(last_run))
    except ValueError:
        return floor


def _parse_update_date(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def pick_redirect(items: list[dict]) -> str:
    """Send 'read more' to the piece the brief singled out."""
    for item in items:
        if item.get("read_deeper") and item.get("url"):
            return item["url"]
    for item in items:
        if item.get("url"):
            return item["url"]
    return DEFAULT_REDIRECT


def build_brief(emails, provider: str | None = None) -> dict:
    now = datetime.now(timezone.utc)

    if emails:
        result = summarize.summarize(emails, provider=provider)
        headline = result["headline"]
        script = result["script"]
        items = result["items"]
        usage = result.get("usage", {})
    else:
        headline = "A quiet morning — nothing new in the inbox"
        script = QUIET_SCRIPT
        items = []
        usage = {}

    main_text, was_cut = summarize.enforce_limit(summarize.to_plain_text(script))

    return {
        "date": now.strftime("%Y-%m-%d"),
        "uid": f"urn:uuid:biopharma-briefing-{now:%Y-%m-%d}",
        "update_date": now.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
        "email_count": len(emails),
        "headline": headline,
        "main_text": main_text,
        "truncated": was_cut,
        "word_count": summarize.word_count(main_text),
        "duration_estimate_sec": summarize.spoken_seconds(main_text),
        "redirection_url": pick_redirect(items),
        "items": items,
        "usage": usage,
    }


def build_feed(brief: dict) -> list[dict]:
    """Prepend today's item to the rolling feed, newest first."""
    existing: list[dict] = []
    if FEED_PATH.exists():
        try:
            loaded = json.loads(FEED_PATH.read_text())
            if isinstance(loaded, list):
                existing = loaded
        except json.JSONDecodeError:
            print(f"warning: {FEED_PATH.name} was not valid JSON — starting a new feed")

    item = {
        "uid": brief["uid"],
        "updateDate": brief["update_date"],
        "titleText": TITLE_TEXT,
        "mainText": brief["main_text"],
        "redirectionUrl": brief["redirection_url"],
    }

    # A same-day rerun replaces its own item rather than duplicating the uid.
    kept = [entry for entry in existing if entry.get("uid") != item["uid"]]

    stale_floor = datetime.now(timezone.utc) - timedelta(days=FEED_ITEM_MAX_AGE_DAYS)
    fresh = []
    for entry in kept:
        parsed = _parse_update_date(entry.get("updateDate", ""))
        if parsed is None or parsed >= stale_floor:
            fresh.append(entry)

    return [item, *fresh][:MAX_FEED_ITEMS]


def render_digest(brief: dict, emails) -> str:
    seconds = brief["duration_estimate_sec"]
    lines = [
        f"# Biopharma brief — {brief['date']}",
        "",
        f"**{brief['headline']}**",
        "",
        f"{brief['email_count']} newsletters · {brief['word_count']} words · "
        f"about {seconds // 60}m{seconds % 60:02d}s aloud",
        "",
    ]

    if brief["items"]:
        lines += ["## Stories", ""]
        for item in brief["items"]:
            title = item["title"]
            marker = " ⭐" if item.get("read_deeper") else ""
            heading = f"[{title}]({item['url']})" if item["url"] else title
            lines += [f"### {heading}{marker}", "", item["why_it_matters"], "", f"*{item['source']}*", ""]

    lines += ["## Script", "", brief["main_text"], ""]

    if emails:
        lines += ["## Sources", ""]
        lines += [f"- {e.subject} — {e.sender}" for e in emails]
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print, write nothing.")
    # `or` not `get(default)`: an unset GitHub Actions variable arrives as "".
    parser.add_argument(
        "--folder", default=os.environ.get("GMAIL_FOLDER") or fetch.DEFAULT_FOLDER
    )
    parser.add_argument("--hours", type=int, default=26)
    parser.add_argument("--limit", type=int, help="Only inspect the newest N messages.")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "gemini"],
        help="Override LLM_PROVIDER. Handy for running both on the same morning.",
    )
    args = parser.parse_args()

    state = load_state()
    cutoff = cutoff_from(state, args.hours)
    seen = set(state.get("seen_message_ids", []))

    emails = fetch.fetch(args.folder, cutoff, seen_ids=seen, limit=args.limit)
    print(f"{len(emails)} new email(s) since {cutoff:%Y-%m-%d %H:%M} UTC")

    brief = build_brief(emails, provider=args.provider)

    print()
    print(brief["headline"])
    print("-" * len(brief["headline"]))
    print()
    print(brief["main_text"])
    print()
    print(
        f"{brief['word_count']} words · about {brief['duration_estimate_sec']}s aloud "
        f"· {len(brief['main_text'])} of {summarize.MAX_MAINTEXT_CHARS} characters"
    )
    print(f"read more → {brief['redirection_url']}")
    if brief["truncated"]:
        print(
            "warning: the script ran past Alexa's 4,500-character limit and was cut "
            "at a sentence. Tighten the LENGTH section of SYSTEM_PROMPT."
        )
    if brief["usage"]:
        print(
            f"{brief['usage'].get('model')} · "
            f"{brief['usage'].get('input_tokens')} tokens in, "
            f"{brief['usage'].get('output_tokens')} out"
        )

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return

    feed = build_feed(brief)
    FEED_PATH.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n")
    DIGEST_PATH.write_text(render_digest(brief, emails))
    save_state(state, emails)
    print(f"\nwrote {FEED_PATH.name} ({len(feed)} item(s)), {DIGEST_PATH.name}, {STATE_PATH.name}")


if __name__ == "__main__":
    main()
