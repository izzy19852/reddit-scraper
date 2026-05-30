# Reddit Scraper

Daily Reddit digest → Notion (Chorrus buyer-signal edition).

Scrapes `old.reddit.com` server-rendered search HTML across a tiered set of
subreddits and query sets, dedupes and ranks posts, runs LLM summarization,
and renders a structured digest into Notion.

## Files

| File | Purpose |
|------|---------|
| `reddit_digest.py` | Main program — pull, dedupe, rank, summarize, write to Notion |
| `reddit_search.py` | Reddit HTML search helper |
| `_probe_envelope.py` | Diagnostic probe |
| `reddit_digest_run.cmd` | Runner for scheduled execution |

## Credentials

Read from environment variables (nothing hardcoded):

- `NOTION_TOKEN` — Notion internal-integration token (required to write)
- `NOTION_PARENT_PAGE_ID` — parent page UUID (optional)

## Usage

```
python reddit_digest.py                 # full run
python reddit_digest.py --dry-run       # print pull stats, skip Notion
python reddit_digest.py --no-summary    # skip LLM analysis
python reddit_digest.py --limit 25      # results per (sub, query)
python reddit_digest.py --max-subs 3    # test: only first N subreddits
```

## Notes

Generated digests (`reddit_digest_*.json`, `reddit_summary_*.json`) and logs
are not version-controlled — see `.gitignore`.
