#!/usr/bin/env python3
"""
manage.py — Manual curation tool for the AI news feed.

Commands:
  add <url>       Fetch title/description from URL, add directly to stories
  remove <id>     Remove a story by ID (searches both stories and pending)
  approve <id>    Move a story from pending → stories
  list-pending    Print all pending stories in readable format

Usage: python scripts/manage.py <command> [args]
Dependencies: requests, pyyaml
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

# ---------------------------------------------------------------------------
# Paths  (mirrors fetch_and_filter.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
DATA_PATH = REPO_ROOT / "data" / "stories.json"

# ---------------------------------------------------------------------------
# Config & data helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"ERROR: config.yaml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_stories() -> dict:
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            return json.load(f)
    return {"last_updated": "", "stories": [], "pending": []}


def save_stories(store: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DATA_PATH, "w") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def make_story_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def all_ids(store: dict) -> set:
    return {s["id"] for s in store["stories"]} | {s["id"] for s in store["pending"]}


# ---------------------------------------------------------------------------
# Lightweight HTML metadata extractor  (stdlib html.parser + requests)
# ---------------------------------------------------------------------------

class _MetaParser(HTMLParser):
    """Extracts <title> and the best available meta description from HTML."""

    def __init__(self):
        super().__init__()
        self.title: str = ""
        self.description: str = ""
        self._in_title: bool = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs_dict = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            content = attrs_dict.get("content", "").strip()
            # Prefer explicit description; fall back to OG description
            if name == "description" and not self.description:
                self.description = content
            elif prop == "og:description" and not self.description:
                self.description = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()


def fetch_page_meta(url: str) -> tuple[str, str]:
    """
    GET the page at `url` and return (title, description).
    Falls back gracefully if either field is missing.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ai-news-curator/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"ERROR: Could not fetch {url}\n  {exc}")

    parser = _MetaParser()
    parser.feed(resp.text)

    title = parser.title or "(no title found)"
    description = parser.description or ""
    return title, description


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_add(url: str) -> None:
    """Fetch metadata from URL and add directly to stories (bypasses filter)."""
    store = load_stories()
    sid = make_story_id(url)

    if sid in all_ids(store):
        print(f"Story already exists in feed (id: {sid}). Nothing added.")
        return

    print(f"Fetching: {url}")
    title, description = fetch_page_meta(url)

    # Derive a readable source name from the hostname
    host = urlparse(url).netloc.removeprefix("www.")

    story = {
        "id": sid,
        "date": datetime.now(timezone.utc).date().isoformat(),
        "headline": title,
        "source": host,
        "url": url,
        "summary": description[:400],
        "reason": "Manually added via manage.py",
    }

    # Prepend so it surfaces first in the feed
    store["stories"].insert(0, story)
    save_stories(store)

    print(f"  Added  [{sid}] {title}")


def cmd_remove(story_id: str) -> None:
    """Remove a story by ID from stories or pending."""
    store = load_stories()

    before = len(store["stories"]) + len(store["pending"])
    store["stories"] = [s for s in store["stories"] if s["id"] != story_id]
    store["pending"] = [s for s in store["pending"] if s["id"] != story_id]
    after = len(store["stories"]) + len(store["pending"])

    if before == after:
        print(f"No story found with id: {story_id}")
        return

    save_stories(store)
    print(f"Removed [{story_id}]")


def cmd_approve(story_id: str) -> None:
    """Move a story from pending → stories."""
    store = load_stories()

    matches = [s for s in store["pending"] if s["id"] == story_id]
    if not matches:
        # Give a helpful message if it's already in the main feed
        if any(s["id"] == story_id for s in store["stories"]):
            print(f"Story [{story_id}] is already in the main feed.")
        else:
            print(f"No pending story found with id: {story_id}")
        return

    story = matches[0]
    store["pending"] = [s for s in store["pending"] if s["id"] != story_id]
    # Prepend to stories so it appears near the top
    store["stories"].insert(0, story)
    save_stories(store)

    print(f"Approved [{story_id}] → stories")
    print(f"  Headline: {story['headline']}")


def cmd_list_pending() -> None:
    """Print all pending stories in a readable format."""
    store = load_stories()
    pending = store.get("pending", [])

    if not pending:
        print("No stories pending review.")
        return

    print(f"{len(pending)} pending {'story' if len(pending) == 1 else 'stories'}:\n")
    for i, story in enumerate(pending, 1):
        print(f"  [{i}] id:       {story['id']}")
        print(f"       date:     {story.get('date', 'unknown')}")
        print(f"       source:   {story.get('source', 'unknown')}")
        print(f"       headline: {story.get('headline', '(none)')}")
        print(f"       url:      {story.get('url', '')}")
        print(f"       reason:   {story.get('reason', '')}")
        print()

    print("To approve: python scripts/manage.py approve <id>")
    print("To remove:  python scripts/manage.py remove <id>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    load_config()  # Validate config is present and readable at startup

    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Manual curation tool for the AI good-news feed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a URL directly to stories")
    p_add.add_argument("url", help="URL of the story to add")

    p_remove = sub.add_parser("remove", help="Remove a story by ID")
    p_remove.add_argument("id", help="Story ID (16-char hex)")

    p_approve = sub.add_parser("approve", help="Move a pending story to the main feed")
    p_approve.add_argument("id", help="Story ID (16-char hex)")

    sub.add_parser("list-pending", help="Print all pending stories")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args.url)
    elif args.command == "remove":
        cmd_remove(args.id)
    elif args.command == "approve":
        cmd_approve(args.id)
    elif args.command == "list-pending":
        cmd_list_pending()


if __name__ == "__main__":
    main()
