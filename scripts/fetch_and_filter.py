#!/usr/bin/env python3
"""
fetch_and_filter.py — Daily pipeline: fetch news → keyword pre-filter →
LLM filter → write accepted stories to data/stories.json.

Dependencies: anthropic, feedparser, requests, pyyaml
Usage: python scripts/fetch_and_filter.py
Secrets expected as env vars: ANTHROPIC_API_KEY
Optional:                      NEWS_API_KEY, GUARDIAN_API
(Hacker News and RSS feeds require no keys.)
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser
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

EMPTY_STORE: dict = {"last_updated": "", "stories": []}


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
    """Drop stories older than max_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).date().isoformat()
    before = len(store["stories"])
    store["stories"] = [s for s in store["stories"] if s.get("date", "") >= cutoff]
    pruned = before - len(store["stories"])
    if pruned:
        print(f"[prune] Removed {pruned} stories older than {max_days} days")


def existing_ids(store: dict) -> set:
    """Return the set of all story IDs already in the store."""
    return {s["id"] for s in store["stories"]}


def make_story_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Normalised article structure
#
# All fetch functions return a list of dicts with these keys:
#   title       str
#   url         str
#   description str   (may be empty for HN)
#   source      dict  {"name": str}
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# NewsAPI fetch
# ---------------------------------------------------------------------------

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Deliberately broad — the keyword pre-filter and LLM do the real filtering.
NEWSAPI_QUERY = '"artificial intelligence" OR "machine learning" OR "large language model"'


def fetch_newsapi(api_key: str, lookback_hours: int) -> list[dict]:
    from_dt = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "q": NEWSAPI_QUERY,
        "from": from_dt,
        "sortBy": "publishedAt",
        "pageSize": 100,
        "language": "en",
        "apiKey": api_key,
    }

    resp = requests.get(NEWSAPI_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "ok":
        print(f"[fetch/newsapi] WARNING: {data.get('message', data)}")
        return []

    articles = data.get("articles", [])
    print(f"[fetch/newsapi] {len(articles)} articles returned")
    return [
        {
            "title": a.get("title") or "",
            "url": a.get("url") or "",
            "description": a.get("description") or a.get("content") or "",
            "source": {"name": (a.get("source") or {}).get("name") or "Unknown"},
        }
        for a in articles
    ]


# ---------------------------------------------------------------------------
# Guardian API fetch
# ---------------------------------------------------------------------------

GUARDIAN_URL = "https://content.guardianapis.com/search"
GUARDIAN_QUERY = "artificial intelligence OR machine learning"


def fetch_guardian(api_key: str, lookback_hours: int) -> list[dict]:
    from_date = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%d")

    params = {
        "q": GUARDIAN_QUERY,
        "from-date": from_date,
        "page-size": 50,
        "show-fields": "trailText",
        "order-by": "newest",
        "api-key": api_key,
    }

    resp = requests.get(GUARDIAN_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("response", {}).get("results", [])
    print(f"[fetch/guardian] {len(results)} articles returned")
    return [
        {
            "title": r.get("webTitle") or "",
            "url": r.get("webUrl") or "",
            "description": (r.get("fields") or {}).get("trailText") or "",
            "source": {"name": "The Guardian"},
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Hacker News fetch (via Algolia search API — no key required)
# ---------------------------------------------------------------------------

HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_QUERY = "artificial intelligence"


def fetch_hn(lookback_hours: int) -> list[dict]:
    cutoff = int(
        (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp()
    )

    params = {
        "query": HN_QUERY,
        "tags": "story",
        "numericFilters": f"created_at_i>{cutoff}",
        "hitsPerPage": 50,
    }

    resp = requests.get(HN_SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])

    articles = []
    for h in hits:
        url = h.get("url") or ""
        if not url:
            continue  # skip self/Ask HN posts without an external link
        articles.append({
            "title": h.get("title") or "",
            "url": url,
            "description": "",  # HN hits don't carry article summaries
            "source": {"name": "Hacker News"},
        })

    print(f"[fetch/hn] {len(articles)} articles returned (of {len(hits)} hits)")
    return articles


# ---------------------------------------------------------------------------
# RSS feed fetch (no key required)
# ---------------------------------------------------------------------------

HTML_TAG_RE = re.compile(r"<[^>]+>")


def fetch_rss_feeds(feed_urls: list[str], lookback_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    articles = []

    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
            source_name = feed.feed.get("title") or feed_url
            count = 0

            for entry in feed.entries:
                url = entry.get("link") or ""
                if not url:
                    continue

                # Filter by publication date when available
                if entry.get("published_parsed"):
                    pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    if pub < cutoff:
                        continue

                summary = entry.get("summary") or entry.get("description") or ""
                summary = HTML_TAG_RE.sub("", summary).strip()

                articles.append({
                    "title": entry.get("title") or "",
                    "url": url,
                    "description": summary[:500],
                    "source": {"name": source_name},
                })
                count += 1

            print(f"[fetch/rss] {count} articles from {source_name}")
        except Exception as exc:
            print(f"[fetch/rss] ERROR fetching {feed_url}: {exc}")

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

def parse_llm_response(text: str) -> tuple[str, str, str, str]:
    """
    Parse the structured prompt response into (decision, reason, confidence, tag).
    Returns empty strings for any field that can't be parsed.
    """
    decision = reason = confidence = tag = ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif line.upper().startswith("CONFIDENCE:"):
            confidence = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("TAG:"):
            tag = line.split(":", 1)[1].strip()
    return decision, reason, confidence, tag


def filter_with_llm(
    articles: list[dict],
    prompt_template: str,
    model: str,
    max_stories: int,
    known_ids: set,
) -> list[dict]:
    """
    Evaluate each article with the LLM.

    Returns:
        accepted  — ACCEPT + HIGH (up to max_stories)
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    accepted: list[dict] = []
    today = datetime.now(timezone.utc).date().isoformat()

    for article in articles:
        if len(accepted) >= max_stories:
            break

        url = article.get("url") or ""
        sid = make_story_id(url)

        if sid in known_ids:
            print(f"  [skip/dup] {article.get('title', '')[:70]}")
            continue

        headline = (article.get("title") or "").strip()
        source = ((article.get("source") or {}).get("name") or "Unknown").strip()
        summary = (article.get("description") or "").strip()

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

        decision, reason, confidence, tag = parse_llm_response(response_text)
        label = f"{decision}/{confidence}" if decision else "PARSE_ERROR"
        print(f"  [{label}] [{tag or '?'}] {headline[:60]}")

        story = {
            "id": sid,
            "date": today,
            "headline": headline,
            "source": source,
            "url": url,
            "summary": summary[:400],
            "reason": reason,
            "tag": tag or "Other",
        }

        if decision == "ACCEPT" and confidence == "HIGH":
            accepted.append(story)
            known_ids.add(sid)
        # REJECT, MEDIUM, and anything unparseable → silently discard

    return accepted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    prompt_template = load_prompt(config["active_prompt"])
    model: str = config["model"]
    max_stories: int = config["max_stories_per_day"]
    lookback_hours: int = config["lookback_hours"]

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

    # ------------------------------------------------------------------
    # Fetch from all sources
    # ------------------------------------------------------------------
    all_articles: list[dict] = []

    news_api_key = os.environ.get("NEWS_API_KEY")
    if news_api_key:
        all_articles += fetch_newsapi(news_api_key, lookback_hours)
    else:
        print("[fetch/newsapi] NEWS_API_KEY not set — skipping")

    guardian_api_key = os.environ.get("GUARDIAN_API")
    if guardian_api_key:
        all_articles += fetch_guardian(guardian_api_key, lookback_hours)
    else:
        print("[fetch/guardian] GUARDIAN_API not set — skipping")

    all_articles += fetch_hn(lookback_hours)

    rss_feeds: list[str] = config.get("rss_feeds", [])
    if rss_feeds:
        all_articles += fetch_rss_feeds(rss_feeds, lookback_hours)

    # Deduplicate by URL before any filtering
    seen_urls: set[str] = set()
    articles: list[dict] = []
    for a in all_articles:
        url = a.get("url") or ""
        if url and url not in seen_urls:
            seen_urls.add(url)
            articles.append(a)

    print(
        f"\n[fetch] {len(articles)} unique articles from all sources"
        f" ({len(all_articles)} total before dedup)"
    )

    # ------------------------------------------------------------------
    # Pre-filter → LLM filter
    # ------------------------------------------------------------------
    articles = keyword_prefilter(articles)

    print(f"\n[llm] Evaluating {len(articles)} candidates with {model}...")
    accepted = filter_with_llm(
        articles, prompt_template, model, max_stories, known
    )

    # Prepend so newest stories appear first
    store["stories"] = accepted + store["stories"]

    save_stories(store)

    print()
    print("=== Results ===")
    print(f"  Accepted (HIGH):  {len(accepted)}")
    print(f"  Total in feed:    {len(store['stories'])}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"stories_added={len(accepted)}\n")


if __name__ == "__main__":
    main()
