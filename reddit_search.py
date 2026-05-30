"""Search Reddit by keyword and print the top results.

Usage:
    python reddit_search.py "your search query" [--limit 25] [--sort relevance]

Sort options: relevance, hot, top, new, comments
"""
import argparse
import sys
import textwrap
from datetime import datetime, timezone

import requests

USER_AGENT = "python:reddit_search:0.1 (by /u/anonymous)"
SEARCH_URL = "https://www.reddit.com/search.json"
VALID_SORTS = {"relevance", "hot", "top", "new", "comments"}


def search(query: str, limit: int = 25, sort: str = "relevance") -> list[dict]:
    params = {"q": query, "limit": limit, "sort": sort, "raw_json": 1}
    resp = requests.get(
        SEARCH_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    return [child["data"] for child in resp.json()["data"]["children"]]


def format_post(i: int, post: dict) -> str:
    created = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc)
    preview = (post.get("selftext") or "").strip().replace("\n", " ")
    if preview:
        preview = textwrap.shorten(preview, width=200, placeholder="...")
    return (
        f"[{i}] {post['title']}\n"
        f"    r/{post['subreddit']}  u/{post['author']}  "
        f"score={post['score']}  comments={post['num_comments']}  "
        f"{created:%Y-%m-%d %H:%M UTC}\n"
        f"    https://reddit.com{post['permalink']}"
        + (f"\n    {preview}" if preview else "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Search Reddit by keyword.")
    parser.add_argument("query", help="search query")
    parser.add_argument("--limit", type=int, default=25, help="number of results (max 100)")
    parser.add_argument(
        "--sort",
        default="relevance",
        choices=sorted(VALID_SORTS),
        help="sort order",
    )
    args = parser.parse_args()

    try:
        posts = search(args.query, limit=args.limit, sort=args.sort)
    except requests.HTTPError as e:
        print(f"Reddit API error: {e}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 1

    if not posts:
        print("No results.")
        return 0

    print(f"{len(posts)} result(s) for {args.query!r} (sort={args.sort}):\n")
    for i, post in enumerate(posts, 1):
        print(format_post(i, post))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
