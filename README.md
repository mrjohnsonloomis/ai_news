# AI for Good — Daily Feed

A static news feed that surfaces genuine, beneficial applications of AI and machine learning. A GitHub Action runs every morning, fetches recent stories, filters them through the Anthropic API against a strict editorial prompt, and commits the results to this repository. GitHub Pages serves the frontend. There is no server.

---

## How it works

```
NewsAPI (last 24h)
    │
    ▼
Keyword pre-filter          ← rejects funding/earnings/acquisition headlines
    │
    ▼
Anthropic API (per story)   ← evaluates against prompts/filter_current.txt
    │
    ├─ ACCEPT + HIGH   ──→  data/stories.json  (published to feed)
    ├─ ACCEPT + MEDIUM ──→  data/stories.json  (pending[], for manual review)
    └─ REJECT          ──→  discarded
    │
    ▼
git commit + push           ← only if something changed
    │
    ▼
GitHub Pages serves index.html, which fetches data/stories.json
```

All runtime settings live in `config.yaml`. The filtering logic lives in `prompts/filter_current.txt`. Neither the pipeline script nor the workflow hardcodes any of these values.

---

## Repository structure

```
/
├── index.html                   Frontend — single file, no build step
├── config.yaml                  Runtime settings for the pipeline
├── data/
│   └── stories.json             Accumulated feed data (committed by the Action)
├── prompts/
│   └── filter_current.txt       Active filtering prompt (LLM editorial criteria)
├── scripts/
│   ├── fetch_and_filter.py      Daily pipeline
│   └── manage.py                Manual curation CLI
├── .github/
│   └── workflows/
│       └── daily.yml            Scheduled GitHub Action
└── README.md
```

---

## Setup

### 1. Fork or create the repository

Create a new repository on GitHub using this code as the starting point. Make sure it's the repository you intend to publish from — GitHub Pages will serve directly from the `main` branch root.

### 2. Get API keys

**NewsAPI** — [newsapi.org](https://newsapi.org)
- Register for a free account
- Copy your API key from the dashboard
- The free tier returns up to 100 articles per request, which is sufficient for daily runs

**Anthropic API** — [console.anthropic.com](https://console.anthropic.com)
- Create an account and generate an API key
- The pipeline uses `claude-sonnet-4-20250514` by default (configurable in `config.yaml`)
- Each daily run makes one API call per candidate story that passes the keyword pre-filter — typically 20–60 calls

### 3. Add GitHub Secrets

In your repository: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name        | Value                        |
|--------------------|------------------------------|
| `NEWS_API_KEY`     | Your NewsAPI key             |
| `ANTHROPIC_API_KEY`| Your Anthropic API key       |

### 4. Enable GitHub Pages

In your repository: **Settings → Pages**

- Source: **Deploy from a branch**
- Branch: `main` / `(root)`
- Save

GitHub Pages will serve `index.html` from the repository root. The frontend fetches `data/stories.json` at the same origin, so no CORS configuration is needed.

### 5. Seed the data file

The pipeline creates `data/stories.json` automatically on first run, but GitHub Pages needs the file to exist before the first Action runs or the frontend will show a fetch error. Create it manually:

```bash
echo '{"last_updated": "", "stories": [], "pending": []}' > data/stories.json
git add data/stories.json
git commit -m "chore: initialise stories.json"
git push
```

### 6. Trigger a first run

Go to **Actions → Daily feed update → Run workflow** to trigger it manually without waiting for the 8am UTC schedule. Check the run log to confirm both API keys work and stories are being evaluated.

---

## Manual curation

`scripts/manage.py` is a command-line tool for curating the feed outside of the automated pipeline. All commands read `config.yaml` and operate directly on `data/stories.json`.

### Add a story by URL

Fetches the page title and meta description, then adds the story directly to the main feed, bypassing the filter entirely.

```bash
python scripts/manage.py add https://example.com/some-article
```

### Review pending stories

Stories that passed the LLM filter with MEDIUM confidence are held in the `pending` array rather than published immediately.

```bash
python scripts/manage.py list-pending
```

Output:

```
3 stories pending review:

  [1] id:       a3f2c1b0d4e5f678
       date:     2026-03-03
       source:   Nature
       headline: AI model detects early-stage pancreatic cancer in CT scans...
       url:      https://...
       reason:   Demonstrates measurable clinical accuracy in a peer-reviewed trial.
```

### Approve a pending story

Moves the story from `pending` into the main `stories` array so it appears in the feed.

```bash
python scripts/manage.py approve a3f2c1b0d4e5f678
```

### Remove a story

Removes by ID from either `stories` or `pending` — no need to know which array it's in.

```bash
python scripts/manage.py remove a3f2c1b0d4e5f678
```

After any `manage.py` command that changes `data/stories.json`, commit and push the file:

```bash
git add data/stories.json
git commit -m "curate: approve story on pancreatic cancer detection"
git push
```

---

## Configuration

`config.yaml` controls all runtime behaviour of the pipeline:

```yaml
active_prompt: prompts/filter_current.txt   # which prompt file to use
max_stories_per_day: 10                     # cap on HIGH-confidence accepts per run
lookback_hours: 24                          # how far back to search in NewsAPI
model: claude-sonnet-4-20250514             # Anthropic model to use for filtering
```

Changes take effect on the next pipeline run. No script edits required.

---

## Swapping prompt files

The active prompt is whatever file `active_prompt` points to in `config.yaml`. To experiment with a stricter or more permissive filter without losing the current one:

1. Create a new prompt file:
   ```bash
   cp prompts/filter_current.txt prompts/filter_strict.txt
   # edit prompts/filter_strict.txt
   ```

2. Point `config.yaml` at it:
   ```yaml
   active_prompt: prompts/filter_strict.txt
   ```

3. Commit both files and push. The next pipeline run uses the new prompt.

To revert, change `active_prompt` back — old prompt files are never deleted automatically.

---

## Running the pipeline locally

Useful for testing prompt changes or debugging before they run in CI.

```bash
# Install dependencies
pip install anthropic requests pyyaml

# Set credentials
export NEWS_API_KEY=your_key_here
export ANTHROPIC_API_KEY=your_key_here

# Run the pipeline against your local data/stories.json
python scripts/fetch_and_filter.py
```

The script reads `config.yaml` from the repository root, so run it from the repo root directory. It will write results to `data/stories.json` exactly as the Action does. Review the output, then commit the file if you're happy with the results.

To test without spending API credits, you can temporarily point `active_prompt` at a prompt file that always returns `REJECT` — the script will run the full fetch and pre-filter, then discard everything cleanly.

---

## Keeping the feed current

The Action runs at 08:00 UTC daily. Stories older than 90 days are pruned automatically on each run. The feed accumulates up to 90 days of history — older entries fall off as new ones are added.

If the filter rejects everything on a given day (which is a valid outcome by design), no commit is made and the feed is unchanged. The frontend handles an empty `stories` array gracefully.
