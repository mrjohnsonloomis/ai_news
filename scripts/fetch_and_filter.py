#!/usr/bin/env python3
"""
fetch_and_filter.py — Daily pipeline: fetch news → keyword pre-filter →
LLM filter → write accepted stories to data/stories.json.

Dependencies: anthropic, requests, pyyaml
Usage: python scripts/fetch_and_filter.py
Secrets expected as env vars: NEWS_API_KEY, ANTHROPIC_API_KEY
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
DATA_PATH = REPO_ROOT / "data" / "stories.json"

# ---------------------------------------------------------------------------
# Config & prompt loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_prompt(prompt_path: str) -> str:
    full_path = REPO_ROOT / prompt_path
    if not full_path.exists():
        sys.exit(f"ERROR: Prompt file not found: {full_path}")
    with open(full_path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# stories.json helpers
# ---------------------------------------------------------------------------

EMPTY_STORE: dict = {"last_updated": "", "stories": [], "pending": []}


def load_stories() -> dict:
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            return json.load(f)
    return {k: v for k, v in EMPTY_STORE.items()}


def save_stories(store: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DATA_PATH, "w") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def prune_old_stories(store: dict, max_days: int = 90) -> None:
    """Drop stories older than max_days from both arrays."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).date().isoformat()
    before = len(store["stories"]) + len(store["pending"])
    store["stories"] = [s for s in store["stories"] if s.get("date", "") >= cutoff]
    store["pending"] = [s for s in store["pending"] if s.get("date", "") >= cutoff]
    pruned = before - len(store["stories"]) - len(store["pending"])
    if pruned:
        print(f"[prune] Removed {pruned} stories older than {max_days} days")


def existing_ids(store: dict) -> set:
    """Return the set of all story IDs already in the store (both arrays)."""
    ids = {s["id"] for s in store["stories"]}
    ids |= {s["id"] for s in store["pending"]}
    return ids


def make_story_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# NewsAPI fetch
# ---------------------------------------------------------------------------

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Deliberately broad — the keyword pre-filter and LLM do the real filtering.
# A complex AND query on the free tier reliably returns 0 results.
SEARCH_QUERY = '"artificial intelligence" OR "machine learning" OR "large language model"'


def fetch_candidates(api_key: str, lookback_hours: int) -> list[dict]:
    from_dt = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "q": SEARCH_QUERY,
        "from": from_dt,
        "sortBy": "publishedAt",  # relevancy sort is restricted on the free tier
        "pageSize": 100,          # max allowed; stays within free-tier total cap
        "language": "en",
        "apiKey": api_key,
    }

    resp = requests.get(NEWSAPI_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "ok":
        sys.exit(f"ERROR: NewsAPI returned error: {data.get('message', data)}")

    articles = data.get("articles", [])
    print(f"[fetch] {len(articles)} articles returned by NewsAPI")
    return articles


# ---------------------------------------------------------------------------
# Keyword pre-filter  (cheap — runs before any LLM calls)
# ---------------------------------------------------------------------------

# Matches financial / business-hype terms in headlines.
REJECT_PATTERN = re.compile(
    r"\b("
    r"funding|raises|raised|valuation|ipo|acquisition|acquires|acquired"
    r"|merger|earnings|stock|shares|investor|investors|venture|seed round"
    r"|series [a-e]|unicorn|market cap|revenue|profit|quarterly|fiscal"
    r"|layoff|layoffs|bankruptcy"
    r")\b",
    re.IGNORECASE,
)


def keyword_prefilter(articles: list[dict]) -> list[dict]:
    passed, rejected = [], 0
    for article in articles:
        headline = article.get("title") or ""

        # NewsAPI marks removed articles this way
        if "[Removed]" in headline or not headline.strip():
            rejected += 1
            continue

        # Skip articles with no usable URL
        url = article.get("url") or ""
        if not url or "removed" in url.lower():
            rejected += 1
            continue

        if REJECT_PATTERN.search(headline):
            rejected += 1
            continue

        passed.append(article)

    print(f"[prefilter] {rejected} rejected by keyword filter, {len(passed)} remain")
    return passed


# ---------------------------------------------------------------------------
# LLM filter
# ---------------------------------------------------------------------------

def parse_llm_response(text: str) -> tuple[str, str, str]:
    """
    Parse the structured prompt response into (decision, reason, confidence).
    Returns empty strings for any field that can't be parsed.
    """
    decision = reason = confidence = ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif line.upper().startswith("CONFIDENCE:"):
            confidence = line.split(":", 1)[1].strip().upper()
    return decision, reason, confidence


def filter_with_llm(
    articles: list[dict],
    prompt_template: str,
    model: str,
    max_stories: int,
    known_ids: set,
) -> tuple[list[dict], list[dict]]:
    """
    Evaluate each article with the LLM.

    Returns:
        accepted  — ACCEPT + HIGH (up to max_stories)
        pending   — ACCEPT + MEDIUM (no cap; for manual review)

    Continues evaluating all candidates even after max_stories is reached
    so MEDIUM-confidence stories aren't lost.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    accepted: list[dict] = []
    pending: list[dict] = []
    today = datetime.now(timezone.utc).date().isoformat()

    for article in articles:
        url = article.get("url") or ""
        sid = make_story_id(url)

        if sid in known_ids:
            print(f"  [skip/dup] {article.get('title', '')[:70]}")
            continue

        headline = (article.get("title") or "").strip()
        source = ((article.get("source") or {}).get("name") or "Unknown").strip()
        # Prefer description over content; content is often truncated by NewsAPI
        summary = (article.get("description") or article.get("content") or "").strip()

        prompt = prompt_template.format(
            headline=headline,
            source=source,
            summary=summary,
        )

        try:
            message = client.messages.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text
        except Exception as exc:
            print(f"  [llm-error] {exc} — skipping '{headline[:60]}'")
            continue

        decision, reason, confidence = parse_llm_response(response_text)
        label = f"{decision}/{confidence}" if decision else "PARSE_ERROR"
        print(f"  [{label}] {headline[:70]}")

        story = {
            "id": sid,
            "date": today,
            "headline": headline,
            "source": source,
            "url": url,
            "summary": summary[:400],
            "reason": reason,
        }

        if decision == "ACCEPT" and confidence == "HIGH":
            if len(accepted) < max_stories:
                accepted.append(story)
                known_ids.add(sid)   # prevent double-count if URL appears twice
        elif decision == "ACCEPT" and confidence == "MEDIUM":
            pending.append(story)
            known_ids.add(sid)
        # REJECT and anything unparseable → silently discard

    return accepted, pending


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    prompt_template = load_prompt(config["active_prompt"])
    model: str = config["model"]
    max_stories: int = config["max_stories_per_day"]
    lookback_hours: int = config["lookback_hours"]

    news_api_key = os.environ.get("NEWS_API_KEY")
    if not news_api_key:
        sys.exit("ERROR: NEWS_API_KEY environment variable not set.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: ANTHROPIC_API_KEY environment variable not set.")

    print("=== fetch_and_filter.py ===")
    print(f"  Model:        {model}")
    print(f"  Prompt:       {config['active_prompt']}")
    print(f"  Lookback:     {lookback_hours}h")
    print(f"  Max stories:  {max_stories}")
    print()

    # Load existing data
    store = load_stories()
    prune_old_stories(store)
    known = existing_ids(store)

    # Fetch → pre-filter → LLM filter
    articles = fetch_candidates(news_api_key, lookback_hours)
    articles = keyword_prefilter(articles)

    print(f"\n[llm] Evaluating {len(articles)} candidates with {model}...")
    accepted, pending = filter_with_llm(
        articles, prompt_template, model, max_stories, known
    )

    # Prepend so newest stories appear first
    store["stories"] = accepted + store["stories"]
    store["pending"] = pending + store["pending"]

    save_stories(store)

    print()
    print("=== Results ===")
    print(f"  Accepted (HIGH):    {len(accepted)}")
    print(f"  Pending  (MEDIUM):  {len(pending)}")
    print(f"  Total in feed:      {len(store['stories'])}")
    print(f"  Total pending:      {len(store['pending'])}")

    # Expose counts as GitHub Actions step outputs so the workflow can
    # build a meaningful commit message without re-parsing the JSON.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"stories_added={len(accepted)}\n")
            f.write(f"pending_added={len(pending)}\n")


if __name__ == "__main__":
    main()
