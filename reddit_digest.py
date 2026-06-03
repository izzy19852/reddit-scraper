"""Daily Reddit digest -> Notion (Chorrus buyer-signal edition).

Implements the CHORRUS REDDIT DIGEST spec:
  * SECTION A (this file): tiered subreddit pull x two query sets, per-post
    metadata, dedup, tier-aware quality filter, and a header+posts JSON object.
    Reddit's JSON API is IP-blocked here and OAuth app creation is gated behind
    Devvit, so we scrape old.reddit.com's server-rendered HTML search pages
    (they carry score/comments/author; they do NOT carry post body text).
  * SECTIONS B-G (the LLM): the full spec document is passed to the model as the
    system prompt and the pull JSON as the user message. The model returns a
    structured JSON digest mirroring Section E, which is rendered into Notion.

Env vars:
    NOTION_TOKEN          internal-integration token (required to write)
    NOTION_PARENT_PAGE_ID parent page UUID (optional; defaults to PARENT_PAGE_ID)

Run:
    python reddit_digest.py
    python reddit_digest.py --dry-run          # print pull stats, skip Notion
    python reddit_digest.py --no-summary       # skip LLM analysis
    python reddit_digest.py --limit 25         # results per (sub, query)
    python reddit_digest.py --max-subs 3       # test: only first N subreddits
"""
import argparse
import html as html_lib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# SECTION A.1 — SUBREDDIT TIERS
# ----------------------------------------------------------------------------
TIER1_SUBS = [
    "smallbusiness", "restaurantowners", "sweatystartup", "HVAC", "Plumbing",
    "Welding", "Roofing", "electricians", "Landscaping", "PoolService",
    "AutoBody", "AutoDetailing", "dentistry", "orthodontics", "Optometry",
    "Chiropractic", "PhysicalTherapy", "Veterinary", "medspas", "lawpractice",
    "Bookkeeping",
]
TIER2_SUBS = ["ecommerce", "shopify", "Etsy", "Accounting"]
TIER3_SUBS = [
    "Entrepreneur", "EntrepreneurRideAlong", "startups", "ChatGPT", "OpenAI",
    "artificial", "business",
]

SUBREDDIT_TIER: dict[str, int] = {}
for _s in TIER1_SUBS:
    SUBREDDIT_TIER[_s] = 1
for _s in TIER2_SUBS:
    SUBREDDIT_TIER[_s] = 2
for _s in TIER3_SUBS:
    SUBREDDIT_TIER[_s] = 3
ALL_SUBS = TIER1_SUBS + TIER2_SUBS + TIER3_SUBS

# ----------------------------------------------------------------------------
# SECTION A.2 — QUERY SETS (both sets run in every subreddit)
# ----------------------------------------------------------------------------
QUERY_SET_AI = ["AI", "ChatGPT", "automation", "chatbot"]
# Trimmed to the 8 highest-signal buyer-pain phrases (was 20). Each sub runs
# every query, so this is the dominant request multiplier: cutting the set in
# half takes a full run from 32x24=768 requests down to 32x12=384, with no loss
# of subreddit coverage. The kept phrases still span all three product concepts
# the summarizer scores:
#   phone_agent  -> "missed calls", "after hours", "front desk",
#                   "stuck on the phone"
#   email_agent  -> "answering the same question", "leaking leads"
#   sales_data_qa-> "spreadsheet reconcile", "scattered across"
# Dropped as low-signal/noisy (generic overwhelm or brand names that match far
# more off-topic posts than buyer intent): "tired of answering", "no time",
# "drowning", "leaking bucket", "lost a customer", "lost a lead",
# "doing it myself again", "hire help", "burning out", "another tool",
# "QuickBooks", "Stripe Shopify".
QUERY_SET_PAIN = [
    "missed calls", "after hours", "front desk", "stuck on the phone",
    "answering the same question", "leaking leads", "spreadsheet reconcile",
    "scattered across",
]
QUERY_SETS: list[tuple[str, list[str]]] = [
    ("ai_language", QUERY_SET_AI),
    ("pain_language", QUERY_SET_PAIN),
]

# ----------------------------------------------------------------------------
# SECTION A.3 — promotional-author detection
# ----------------------------------------------------------------------------
PROMO_PHRASES = [
    "i built", "we built", "dm me", "check out my", "i'm working on",
    "im working on", "i will not promote", "[promoting]",
]
# A non-Reddit URL in the body is treated as one promotional marker (proxy for
# "non-Reddit URL to author's own domain" — we cannot verify ownership).
_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
_PROMO_POST_COUNT_THRESHOLD = 3

PARENT_PAGE_ID = "36c4ba8c-a37c-8177-9a80-e11df56285ab"

# Reddit's JSON API is blocked for this IP (unauthenticated), and app creation
# is gated behind Devvit, so OAuth is not an option. old.reddit.com serves
# server-rendered HTML search pages that carry score/comments/author — we scrape
# those. A browser-like User-Agent is required to avoid the bot challenge.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REDDIT_SEARCH = "https://old.reddit.com/r/{sub}/search/"
NOTION_API = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"
# Pacing: unauthenticated JSON endpoints are throttled hard. Go slow and steady
# with jitter, and back off (not just record-and-continue) on a throttle.
SLEEP_BETWEEN_QUERIES = 4.0   # base delay between requests (override with --sleep)
SLEEP_JITTER = 1.5            # add random 0..JITTER on top of every delay
THROTTLE_CODES = {403, 429, 503}
MAX_RETRIES = 4              # retries per request when throttled
BACKOFF_BASE = 20.0         # first backoff; doubles each retry (20, 40, 80, ...)
BACKOFF_MAX = 300.0         # cap a single backoff at 5 min
# Adaptive cooldown: Reddit throttles by requests-per-window, so a fixed delay
# only guesses. Each throttle event permanently raises the base inter-request
# delay for the rest of the run, so the cadence self-tunes below the limit.
ADAPTIVE_STEP = 1.0         # seconds added to every delay per throttle event
ADAPTIVE_MAX = 10.0         # cap on the accumulated adaptive delay
_throttle_state = {"extra": 0.0, "events": 0}
CLAUDE_FALLBACK = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
BODY_EXCERPT_LEN = 500
# LLM call: retry on transient failures, and cap how many posts go to the model
# (a full nightly run can surface 800+ posts, which would overflow the call). We
# send the top-weighted subset for analysis; the Notion appendix lists them all.
LLM_RETRIES = 3
LLM_RETRY_SLEEP = 15.0
LLM_POST_CAP = 120

# Phase-0 concept options (Section C) -> schema keys / Notion labels.
CONCEPTS = [
    ("sales_data_qa", "(1) Ask questions about your sales data"),
    ("phone_agent", "(2) AI that answers your phone"),
    ("email_agent", "(3) AI that manages your email"),
    ("none_other", "(4) None of the above / something else"),
]
QUOTE_TAGS = ["PAIN", "OUTCOME", "OBJECTION", "CONTEXT"]

# ----------------------------------------------------------------------------
# SYSTEM PROMPT — the full spec document (Sections B-G are the LLM's contract).
# Section A is implemented above; it is retained here so the model sees the full
# contract it is validating its input against.
# ----------------------------------------------------------------------------
SPEC_DOC = r"""================================================================================
CHORRUS REDDIT DIGEST — FULL SPEC (Python pull + LLM analysis)
================================================================================
This document is the contract between reddit_digest.py and the LLM that
analyzes its output. The Python pull implements Section A. You receive this
entire document as your system prompt and the Python pull's JSON output as the
user message.

================================================================================
SECTION A — DATA PULL (reddit_digest.py implements this; for your reference)
================================================================================

A.1 SUBREDDIT TIERS — every post is tagged with its tier.
Tier 1 — TRUE BUYER subs (primary signal, weight 3x in analysis):
  r/smallbusiness, r/restaurantowners, r/sweatystartup, r/HVAC, r/Plumbing,
  r/Welding, r/Roofing, r/electricians, r/Landscaping, r/PoolService,
  r/AutoBody, r/AutoDetailing, r/dentistry, r/orthodontics, r/Optometry,
  r/Chiropractic, r/PhysicalTherapy, r/Veterinary, r/medspas, r/lawpractice,
  r/Bookkeeping
Tier 2 — MIXED operator/builder subs (secondary signal, weight 1x):
  r/ecommerce, r/shopify, r/Etsy, r/Accounting
Tier 3 — PEER / BUILDER / EMPLOYEE chatter (atmospheric only, weight 0.5x):
  r/Entrepreneur, r/EntrepreneurRideAlong, r/startups, r/ChatGPT, r/OpenAI,
  r/artificial, r/business

A.2 QUERY SETS — both sets run in every subreddit; results tagged by set.
Set 1 — AI-LANGUAGE (de-emphasized): "AI", "ChatGPT", "automation", "chatbot"
Set 2 — PAIN-LANGUAGE (primary buyer signal): "missed calls", "after hours",
  "front desk", "answering the same question", "stuck on the phone",
  "tired of answering", "no time", "drowning", "leaking bucket",
  "leaking leads", "lost a customer", "lost a lead", "doing it myself again",
  "hire help", "burning out", "another tool", "QuickBooks", "Stripe Shopify",
  "spreadsheet reconcile", "scattered across"
Pain-language queries do NOT require co-occurrence with "AI".

A.3 PER-POST METADATA: anchor, title, subreddit, subreddit_tier, score,
num_comments, url, author, author_post_count_in_pull, author_promotional_flag,
query_used (array), query_set ("ai_language"|"pain_language"; pain wins ties),
body_excerpt (first 500 chars verbatim).

A.4 DEDUPLICATION: one row per post; all query_used recorded; query_set =
"pain_language" if any matching query was pain-language.

A.5 MIN-QUALITY FILTERS: drop posts where score < 2 AND num_comments < 3.
EXCEPTION: never filter Tier 1 subs.

A.6 OUTPUT: a header object (pull_date, total_posts, tier{1,2,3}_posts,
pain_language_posts, ai_language_posts, queries_run, subreddits_queried) plus a
posts array.

================================================================================
SECTION B — INPUT VALIDATION (check before analyzing)
================================================================================
B.1 If tier1_posts < 10: emit the COVERAGE WARNING at the top of the digest.
    Confidence is LOW regardless of total volume.
B.2 If pain_language_posts < 0.3 x ai_language_posts: emit a QUERY-BIAS
    WARNING. The pull is too solution-language-heavy.
B.3 If any A.3 field is missing on >5% of posts: refuse and return only
    "INPUT INVALID — Python pull does not satisfy contract A.3."

================================================================================
SECTION C — CONTEXT
================================================================================
Chorrus is an SMB-focused AI platform pitched at ~$299/mo with per-tenant data
isolation; integrations to QuickBooks, Stripe, Shopify, Drive, etc.; a
sales-data Q&A interface; an AI voice/phone agent; and an AI email agent.
Target buyer: SMB owner running a service business, e-commerce store, or clinic
— with revenue justifying a recurring tooling line item.
Phase 0 discovery tests four options:
  (1) Ask questions about your sales data
  (2) AI that answers your phone
  (3) AI that manages your email
  (4) None of the above / something else
The digest's primary job is to inform whether (1), (2), (3), or (4) is the right
wedge — not to validate that Chorrus is right.

================================================================================
SECTION D — ANTI-SYCOPHANCY RULES (NON-NEGOTIABLE)
================================================================================
D.1 Bear Case is REQUIRED, >=5 cited posts every digest. If <5, state the
    deficit and name it as a likely query/coverage bias.
D.2 Fear of an incumbent (OpenAI bans/leaks, "Microsoft: AI > human cost,"
    Starbucks scraps AI) is NOT buyer intent. Such posts go in "Atmospheric
    Risk" labeled "not pipeline." Never as handraisers.
D.3 Tier 3 sub enthusiasm is builder/peer chatter by default. It does NOT count
    as buyer demand unless the author identifies (in their own post) as an
    operating SMB owner with vertical, team size, and specific operational pain.
D.4 Posts where author_promotional_flag == true are excluded from handraisers.
    Cite them only in "Competitor / Adjacent Build Watch" with [PROMOTIONAL
    AUTHOR].
D.5 Forbidden language: "validates Chorrus's bet", "the wedge is now X", "points
    straight at Chorrus's thesis", any checkmark glyph, or any framing
    presenting one digest as proof. Allowed: "this digest suggested", "evidence
    consistent with", "weak/strong signal that".
D.6 query_set == "pain_language" AND subreddit_tier == 1 is the strongest signal
    in the entire pull. A single such post is worth more than ten Tier 3
    AI-language posts.
D.7 An empty handraiser list is acceptable and informative. DO NOT PAD.

================================================================================
SECTION E — OUTPUT STRUCTURE (semantics; deliver as JSON per the override below)
================================================================================
1. Coverage and Confidence — pull totals w/ tier and query-set breakdowns;
   Confidence HIGH (>=30 tier1) / MEDIUM (10-29) / LOW (<10); coverage warning
   (B.1); query-bias warning (B.2).
2. Bear Case (REQUIRED, >=5 posts) — one sentence per post on why it argues
   against Chorrus (wrong wedge / wrong price / undifferentiated / buyer
   indifference / saturated / direct competitor / cost-ROI red flag / wrong
   target). No softening.
3. Concept-Test Scoreboard — for each of the four Section C options: SUPPORT /
   CONTRADICT / ADJACENT with tallies. Call out Tier1 + pain-language entries as
   high-weight evidence. Most important longitudinal section.
4. True-Buyer Handraisers (STRICT, all four required): (a) promotional_flag ==
   false; (b) tier 1 OR author explicitly identifies as operating SMB owner with
   vertical + team size; (c) describes a SPECIFIC operational pain Chorrus could
   solve; (d) evidence of revenue/team size justifying ~$299/mo. Each entry:
   anchor, which Chorrus surface, what is MISSING so outreach knows what to
   qualify. Empty list is acceptable.
5. Competitor / Adjacent Build Watch — anyone building/selling overlapping
   Chorrus surface (voice agent, SMB AI workspace, virtual CFO, AEO, WhatsApp
   CRM, missed-call recovery). What / which surface / what's distinctive /
   [PROMOTIONAL AUTHOR] where applicable.
6. Atmospheric Risk — incumbent failures, hype skepticism, cost stories,
   regulatory noise. Labeled NOT BUYER INTENT. Each ends with "Implication for
   Chorrus:" (how Chorrus should TALK, not what changes about pipeline).
7. Recurring vs Emerging Themes — RECURRING (also in prior day) vs EMERGING
   (new). Single-day Tier2/3 themes with score <50 are noise unless cited
   elsewhere; Tier 1 themes kept regardless of score.
8. Notable Quotes — verbatim, attributed, linked. Prefer Tier1 + pain-language.
   Tag each [PAIN]/[OUTCOME]/[OBJECTION]/[CONTEXT]. Min 3 from Tier 1 if any
   exist.
9. What To Do This Week — three concrete, post-tied actions, each referencing a
   specific anchor and a specific next step. If <3 possible, state so.

================================================================================
SECTION F — FORMATTING
================================================================================
Reference posts by their anchor (e.g. "#185"). No decorative emoji. Never a
checkmark glyph.

================================================================================
SECTION G — SELF-CHECK BEFORE RETURNING
================================================================================
  [ ] Input contract (B) validated; warnings emitted if triggered
  [ ] Bear Case has >=5 cited posts (or explicit deficit statement)
  [ ] No fear-of-incumbent post in handraisers
  [ ] No author_promotional_flag == true post in handraisers
  [ ] Concept-Test Scoreboard scores all four options
  [ ] All forbidden language (D.5) absent
  [ ] What To Do This Week has 3 post-tied specifics (or deficit statement)
  [ ] Tier 1 + pain-language posts surfaced where they exist
"""

OUTPUT_OVERRIDE = (
    "\n================================================================================\n"
    "OUTPUT OVERRIDE (supersedes Section F's markdown instruction)\n"
    "================================================================================\n"
    "Reply with a SINGLE JSON object only — no prose, no preamble, no markdown\n"
    "code fences. The JSON must conform to the provided schema. Every reference\n"
    "to a post MUST be that post's exact anchor string from the pull (e.g.\n"
    "\"#185\"). Keys map to Section E as follows:\n"
    "  coverage_and_confidence -> Section 1 (set confidence and any warnings;\n"
    "    use empty string \"\" for a warning that is not triggered)\n"
    "  bear_case               -> Section 2 (aim for >=5 entries; if fewer,\n"
    "    say so in coverage_and_confidence.coverage_warning and explain the bias)\n"
    "  concept_test_scoreboard -> Section 3 (all four concept keys present)\n"
    "  true_buyer_handraisers  -> Section 4 (may be empty; never pad)\n"
    "  competitor_watch        -> Section 5 (set promotional=true for\n"
    "    author_promotional_flag posts)\n"
    "  atmospheric_risk        -> Section 6 (each has an 'implication')\n"
    "  recurring_vs_emerging   -> Section 7 (no prior digest in input => put\n"
    "    everything under 'emerging' and leave 'recurring' empty)\n"
    "  notable_quotes          -> Section 8 (verbatim 'text', anchor, tag)\n"
    "  what_to_do_this_week    -> Section 9 (3 entries; each tied to an anchor)\n"
    "If the input fails B.3, return {\"input_invalid\": \"INPUT INVALID — Python\n"
    "pull does not satisfy contract A.3.\"} and nothing else.\n"
)

# NOTE: SPEC_DOC / OUTPUT_OVERRIDE above are the legacy 9-section analyst spec,
# kept for reference only. The live prompt below produces a plain-language,
# four-lens digest (what people ask / supports / works against / real struggle).
SUMMARY_SYSTEM_PROMPT = r"""You read Reddit posts from small-business owners and turn them into a SHORT,
PLAIN-ENGLISH briefing for the founder of Chorrus. The reader is busy and is NOT
a data analyst: in about 90 seconds they must understand what small-business
owners are asking for, what helps Chorrus, what hurts Chorrus, and what these
people are really struggling with underneath.

ABOUT CHORRUS (the lens):
Chorrus is an AI platform for small businesses (~$299/mo) with three product
surfaces: (1) ask questions about your sales/business data, (2) an AI agent that
answers your phone, (3) an AI agent that handles your email. Target buyer: the
owner of a service business, e-commerce store, or clinic with enough revenue to
justify a monthly tool.

THE INPUT:
A JSON object with a `posts` array. Each post has: anchor (e.g. "#35"), title,
subreddit, subreddit_tier (1 = real buyers, 2 = mixed, 3 = peers/builders),
score, num_comments, query_set ("pain_language" = an owner describing pain in
their own words = strongest signal; "ai_language" = matched a buzzword = weaker),
author_promotional_flag (true = the author is selling something), and
body_excerpt (the post text).

HOW TO WEIGH POSTS:
- A Tier-1 post in pain_language is the strongest signal. One of those beats ten
  Tier-3 "AI" posts.
- Tier-3 posts are mostly other builders/employees talking, NOT buyers. Do not
  treat their enthusiasm as demand.
- author_promotional_flag == true means the author is promoting their own
  product. NEVER put them under "what people are asking" or "people to message" —
  they belong (if anywhere) under "works against Chorrus" as competition.
- An owner venting about an incumbent (QuickBooks down, an OpenAI ban, etc.) is
  NOT a buyer asking for Chorrus. Don't dress it up as demand.
- Some pain matches are accidents: a plumber's "leaking leads" is a literal leak;
  "after hours" can just mean a time of day. Ignore those; only use posts where a
  real owner describes a real operational problem.

YOUR JOB — produce these five things, all in PLAIN ENGLISH an owner would use,
never analyst jargon. Banned words/phrases: "overfit", "falsification", "thesis",
"wedge", "ICP", "race-to-the-bottom", "table-stakes", "greenfield", "signal
density", "cohort", "validates". Write like you're explaining it to a friend.

1. gist — 2-3 sentences: the single most important thing this week's posts told
   us. A real paragraph, no lists, no hedging.

2. what_people_are_asking — the recurring REQUESTS, grouped by what they want
   (NOT one bullet per post — merge duplicates). Each item:
     ask = the want in the owner's own voice ("Something to answer my phone so I
       stop losing jobs")
     who = the kind of business asking
     prevalence = how common it was ("5+ posts", "a couple of owners")
     quote = one word-for-word quote
     anchor = the post it came from

3. supports_chorrus — honest reasons the data is GOOD for Chorrus. Each:
     point = one plain sentence; why = one plain sentence; anchor = best example.

4. works_against_chorrus — reasons the data is BAD or HARD for Chorrus
   (competitors giving the same thing away, this buyer being hard to reach,
   owners wanting something else, price resistance, etc.). REQUIRED and must be
   just as real as the support — do NOT soften it. Each: point, why, anchor.

5. real_struggle — the deeper human problem under the surface asks. What are
   these owners actually fighting every day?
     insight = 2-4 plain sentences
     what_it_means_for_chorrus = 1-2 sentences on how Chorrus should TALK to them.

Plus people_to_message — 3-6 specific NON-promotional owners worth reaching out
to. Each: anchor; who (plain description incl. business type/size if known);
what_they_need (plain); and one short quote if there's a good one.

RULES:
- Quotes must be word-for-word from a post's title or body_excerpt.
- Use real anchors only. Never invent posts or quotes.
- A short section is fine if the data is thin. Do NOT pad.
- If almost nothing real is in the pull, say so plainly in `gist`.

OUTPUT: reply with ONE JSON object matching the provided schema. No prose, no
markdown, no code fences.
"""

# On Windows the `claude` launcher is a .CMD wrapper and cmd.exe caps a command
# line at ~8191 chars, so the 10.5K-char spec can't ride on --system-prompt.
# We pass a tiny system string on argv and send the full contract via stdin
# (which has no length limit) ahead of the pull JSON.
SHORT_SYSTEM_PROMPT = (
    "You are a precise B2B market-research analyst producing a buyer-signal "
    "digest. Follow the CONTRACT given at the top of the user message exactly. "
    "Reply with a single JSON object conforming to the provided schema — no "
    "prose, no markdown, no code fences."
)

# ----------------------------------------------------------------------------
# OUTPUT SCHEMA — plain-language, four-lens digest
# ----------------------------------------------------------------------------
_ANCHOR = {"type": "string"}
_ASK = {
    "type": "object",
    "properties": {
        "ask": {"type": "string"},
        "who": {"type": "string"},
        "prevalence": {"type": "string"},
        "quote": {"type": "string"},
        "anchor": _ANCHOR,
    },
    "required": ["ask", "who", "anchor"],
    "additionalProperties": False,
}
_POINT = {
    "type": "object",
    "properties": {
        "point": {"type": "string"},
        "why": {"type": "string"},
        "anchor": _ANCHOR,
    },
    "required": ["point", "why"],
    "additionalProperties": False,
}
_PERSON = {
    "type": "object",
    "properties": {
        "anchor": _ANCHOR,
        "who": {"type": "string"},
        "what_they_need": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["anchor", "who", "what_they_need"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "gist": {"type": "string"},
        "what_people_are_asking": {"type": "array", "items": _ASK},
        "supports_chorrus": {"type": "array", "items": _POINT},
        "works_against_chorrus": {"type": "array", "items": _POINT},
        "real_struggle": {
            "type": "object",
            "properties": {
                "insight": {"type": "string"},
                "what_it_means_for_chorrus": {"type": "string"},
            },
            "required": ["insight", "what_it_means_for_chorrus"],
            "additionalProperties": False,
        },
        "people_to_message": {"type": "array", "items": _PERSON},
    },
    "required": [
        "gist",
        "what_people_are_asking",
        "supports_chorrus",
        "works_against_chorrus",
        "real_struggle",
        "people_to_message",
    ],
    "additionalProperties": False,
}


# ============================================================================
# SECTION A — DATA PULL
# ============================================================================
def load_dotenv() -> None:
    """Load KEY=VALUE pairs from a .env beside the script (does not override
    variables already set in the real environment)."""
    p = Path(__file__).resolve().parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _note_throttle() -> None:
    """Record a throttle event and step up the adaptive delay floor."""
    _throttle_state["events"] += 1
    prev = _throttle_state["extra"]
    _throttle_state["extra"] = min(prev + ADAPTIVE_STEP, ADAPTIVE_MAX)
    if _throttle_state["extra"] != prev:
        print(
            f"    .. adaptive cooldown raised to +{_throttle_state['extra']:.0f}s/request",
            flush=True,
        )


def _polite_sleep(base: float) -> None:
    time.sleep(base + _throttle_state["extra"] + random.uniform(0, SLEEP_JITTER))


# old.reddit search-result markup is stable; parse each result block by hand.
_RESULT_START_RE = re.compile(r'data-fullname="t3_')
_FULLNAME_RE = re.compile(r'data-fullname="t3_([a-z0-9]+)"')
_TITLE_RE = re.compile(r'class="search-title may-blank"[^>]*>(.*?)</a>', re.S)
_PERMA_RE = re.compile(r'href="(/r/[^"]+/comments/[^"]+?)"')
_SCORE_RE = re.compile(r'class="search-score">([\d,]+)\s+point')
_COMMENTS_RE = re.compile(r'class="search-comments may-blank"[^>]*>([\d,]+)\s+comment')
_AUTHOR_RE = re.compile(r'class="author may-blank[^"]*"[^>]*>([^<]+)</a>')


def parse_search_html(html: str) -> list[dict]:
    """Extract posts from an old.reddit search results page.

    Search results carry no selftext, so 'selftext' is empty; the LLM works off
    title + metadata. Returns dicts shaped like the old JSON API rows that
    build_posts() consumes (id, title, selftext, score, num_comments, permalink,
    author).
    """
    starts = [m.start() for m in _RESULT_START_RE.finditer(html)]
    posts: list[dict] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(html)
        block = html[start:end]
        fid = _FULLNAME_RE.search(block)
        if not fid:
            continue
        title_m = _TITLE_RE.search(block)
        perma_m = _PERMA_RE.search(block)
        score_m = _SCORE_RE.search(block)
        comments_m = _COMMENTS_RE.search(block)
        author_m = _AUTHOR_RE.search(block)
        posts.append(
            {
                "id": fid.group(1),
                "name": f"t3_{fid.group(1)}",
                "title": html_lib.unescape(title_m.group(1)).strip() if title_m else "",
                "selftext": "",
                "score": int(score_m.group(1).replace(",", "")) if score_m else 0,
                "num_comments": int(comments_m.group(1).replace(",", "")) if comments_m else 0,
                "permalink": html_lib.unescape(perma_m.group(1)) if perma_m else "",
                "author": author_m.group(1).strip() if author_m else "[deleted]",
                # Self-posts carry a "self" thumbnail; only they have body text.
                "_is_self": "thumbnail self" in block,
            }
        )
    return posts


def _get_with_backoff(
    session: requests.Session, url: str, params: dict | None = None
) -> requests.Response:
    """GET with exponential backoff on throttle codes (403/429/503).

    Honors Retry-After when present, otherwise backs off 20s, 40s, 80s, ...
    (capped). Raises the last error if all retries fail.
    """
    noted = False
    for attempt in range(MAX_RETRIES + 1):
        resp = session.get(url, params=params, timeout=20)
        if resp.status_code in THROTTLE_CODES and attempt < MAX_RETRIES:
            if not noted:  # step the adaptive floor once per request, not per retry
                _note_throttle()
                noted = True
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.strip().isdigit():
                wait = float(retry_after)
            else:
                wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_MAX)
            wait += random.uniform(0, SLEEP_JITTER)
            print(
                f"    .. {resp.status_code} throttled; backing off {wait:.0f}s "
                f"(retry {attempt + 1}/{MAX_RETRIES})",
                flush=True,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()  # exhausted retries
    return resp


def fetch_subreddit(
    session: requests.Session, query: str, subreddit: str, limit: int
) -> list[dict]:
    """Scrape one (sub, query) from old.reddit HTML."""
    params = {
        "q": query,
        "restrict_sr": "on",
        "sort": "top",
        "t": "week",
        "limit": limit,
    }
    resp = _get_with_backoff(session, REDDIT_SEARCH.format(sub=subreddit), params)
    return parse_search_html(resp.text)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_post_body(html: str, post_id: str) -> str:
    """Pull the selftext out of a post's old.reddit page.

    The post's body is the usertext-body md div that follows the hidden
    thing_id input for this t3_ id (the first usertext-body on the page is the
    subreddit sidebar, so we must anchor on the id). HTML is flattened to text.
    """
    m = re.search(
        r'value="t3_' + re.escape(post_id) + r'"[^>]*/>\s*'
        r'<div class="usertext-body[^"]*"[^>]*>\s*<div class="md">(.*?)</div></div>',
        html,
        re.S,
    )
    if not m:
        return ""
    text = _TAG_RE.sub(" ", m.group(1))
    return html_lib.unescape(_WS_RE.sub(" ", text)).strip()


def fetch_post_body(session: requests.Session, post_id: str, permalink: str) -> str:
    # limit=1 keeps the comment tree (and page size) small; we only want the body.
    resp = _get_with_backoff(session, f"https://old.reddit.com{permalink}", {"limit": 1})
    return extract_post_body(resp.text, post_id)


def fetch_bodies(kept: list[dict], sleep_base: float, min_engagement: int = 0) -> None:
    """Populate rec['selftext'] for kept self-posts (one page fetch each).

    With min_engagement > 0, skip body fetches for low-signal posts (score and
    comments both below the bar) to cut request volume; Tier 1 is always kept.
    """
    def wants_body(r: dict) -> bool:
        if not (r.get("_is_self") and r.get("permalink")):
            return False
        if min_engagement <= 0 or r.get("_tier") == 1:
            return True
        return (r.get("score", 0) or 0) >= min_engagement or (
            r.get("num_comments", 0) or 0
        ) >= min_engagement

    targets = [r for r in kept if wants_body(r)]
    skipped = sum(1 for r in kept if r.get("_is_self") and r.get("permalink")) - len(targets)
    if skipped:
        print(f"  (skipping {skipped} low-engagement self-post bodies)", flush=True)
    if not targets:
        return
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    print(f"Fetching bodies for {len(targets)} self-posts...", flush=True)
    for i, rec in enumerate(targets, 1):
        if i > 1:
            _polite_sleep(sleep_base)
        try:
            rec["selftext"] = fetch_post_body(session, rec["id"], rec["permalink"])
        except requests.RequestException as e:
            print(f"    !! body {rec.get('id')}: {e}", flush=True)
        if i % 25 == 0 or i == len(targets):
            print(f"    bodies {i}/{len(targets)}", flush=True)


def fetch_all(
    limit: int,
    max_subs: int | None,
    sleep_base: float,
    subs_filter: list[str] | None = None,
) -> tuple[dict, list[dict], list[str]]:
    """Run both query sets in every subreddit.

    Returns (raw_by_id, errors, subs_queried). raw_by_id is keyed by Reddit post
    id; each value carries the raw post plus accumulated _query_used / _query_set
    / _tier / _sub tags (A.4 dedup happens here).
    """
    if subs_filter:
        subs = [s for s in subs_filter if s in SUBREDDIT_TIER]
    else:
        subs = ALL_SUBS[:max_subs] if max_subs else ALL_SUBS
    raw_by_id: dict[str, dict] = {}
    errors: list[dict] = []
    first = True
    total_pairs = len(subs) * (len(QUERY_SET_AI) + len(QUERY_SET_PAIN))
    done = 0
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    for sub in subs:
        tier = SUBREDDIT_TIER[sub]
        for set_name, queries in QUERY_SETS:
            for query in queries:
                if not first:
                    _polite_sleep(sleep_base)
                first = False
                done += 1
                print(
                    f"[{done}/{total_pairs}] T{tier} r/{sub} {set_name} q={query!r}",
                    flush=True,
                )
                try:
                    posts = fetch_subreddit(session, query, sub, limit)
                except requests.RequestException as e:
                    print(f"    !! {e}", flush=True)
                    errors.append({"subreddit": sub, "query": query, "error": str(e)})
                    continue
                if posts:
                    print(f"    -> {len(posts)} posts", flush=True)
                for p in posts:
                    pid = p.get("id") or p.get("name") or p.get("permalink")
                    if pid is None:
                        continue
                    rec = raw_by_id.get(pid)
                    if rec is None:
                        rec = p
                        rec["_query_used"] = []
                        rec["_query_set"] = "ai_language"
                        rec["_tier"] = tier
                        rec["_sub"] = sub
                        raw_by_id[pid] = rec
                    if query not in rec["_query_used"]:
                        rec["_query_used"].append(query)
                    # pain-language wins ties (A.4)
                    if set_name == "pain_language":
                        rec["_query_set"] = "pain_language"
    return raw_by_id, errors, [f"r/{s}" for s in subs]


def _count_promo_markers(text: str) -> int:
    low = text.lower()
    count = sum(1 for phrase in PROMO_PHRASES if phrase in low)
    # A non-Reddit URL counts as one marker.
    for m in _URL_RE.findall(text):
        host = m.split("//", 1)[-1].lower()
        if "reddit.com" not in host and "redd.it" not in host:
            count += 1
            break
    return count


def filter_posts(raw_by_id: dict) -> list[dict]:
    """SECTION A.5 — min-quality filter (Tier 1 exempt). Returns kept raw recs."""
    kept = []
    for rec in raw_by_id.values():
        tier = rec["_tier"]
        score = rec.get("score", 0) or 0
        comments = rec.get("num_comments", 0) or 0
        if tier != 1 and score < 2 and comments < 3:
            continue
        kept.append(rec)
    return kept


def finalize_posts(kept: list[dict]) -> list[dict]:
    """Compute author counts / promo flags, assign anchors, build A.3 rows.

    Run AFTER fetch_bodies so promo detection and body_excerpt see selftext.
    """
    # author_post_count_in_pull across captured (post-filter) posts.
    author_counts: dict[str, int] = {}
    for rec in kept:
        author_counts[rec.get("author", "")] = author_counts.get(rec.get("author", ""), 0) + 1

    # Order: tier asc, pain before ai, score desc. Then assign anchors.
    def sort_key(r: dict):
        return (r["_tier"], 0 if r["_query_set"] == "pain_language" else 1, -(r.get("score", 0) or 0))

    kept.sort(key=sort_key)

    posts: list[dict] = []
    for i, rec in enumerate(kept, 1):
        author = rec.get("author", "")
        body = rec.get("selftext") or ""
        promo_text = (rec.get("title", "") or "") + "\n" + body
        author_count = author_counts.get(author, 1)
        promo_flag = (
            author_count >= _PROMO_POST_COUNT_THRESHOLD
            or _count_promo_markers(promo_text) >= 2
        )
        permalink = rec.get("permalink", "")
        posts.append(
            {
                "anchor": f"#{i}",
                "title": (rec.get("title") or "").strip(),
                "subreddit": f"r/{rec['_sub']}",
                "subreddit_tier": rec["_tier"],
                "score": rec.get("score", 0) or 0,
                "num_comments": rec.get("num_comments", 0) or 0,
                "url": f"https://reddit.com{permalink}" if permalink else "",
                "author": f"u/{author}" if author else "u/[deleted]",
                "author_post_count_in_pull": author_count,
                "author_promotional_flag": promo_flag,
                "query_used": rec["_query_used"],
                "query_set": rec["_query_set"],
                "body_excerpt": body[:BODY_EXCERPT_LEN],
            }
        )
    return posts


def build_pull(posts: list[dict], subs_queried: list[str], date_str: str) -> dict:
    """SECTION A.6 — header object plus posts array."""
    tier_counts = {1: 0, 2: 0, 3: 0}
    pain = ai = 0
    for p in posts:
        tier_counts[p["subreddit_tier"]] += 1
        if p["query_set"] == "pain_language":
            pain += 1
        else:
            ai += 1
    queries_run = sorted(set(QUERY_SET_AI + QUERY_SET_PAIN))
    return {
        "pull_date": date_str,
        "total_posts": len(posts),
        "tier1_posts": tier_counts[1],
        "tier2_posts": tier_counts[2],
        "tier3_posts": tier_counts[3],
        "pain_language_posts": pain,
        "ai_language_posts": ai,
        "queries_run": queries_run,
        "subreddits_queried": subs_queried,
        "posts": posts,
    }


# ============================================================================
# SECTIONS B-G — LLM analysis
# ============================================================================
def build_summary_prompt(pull: dict) -> str:
    return json.dumps(pull, ensure_ascii=False, indent=2)


def resolve_claude_cmd() -> str:
    found = shutil.which("claude")
    if found:
        return found
    if CLAUDE_FALLBACK.exists():
        return str(CLAUDE_FALLBACK)
    raise RuntimeError(
        "claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
    )


def _parse_json_text(raw: str) -> dict:
    """Best-effort parse of a JSON object from free text (strips code fences,
    falls back to the first balanced {...} block)."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == "{":
                    depth += 1
                elif raw[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[start : i + 1])
                        except json.JSONDecodeError:
                            break
        raise RuntimeError(f"claude output not valid JSON:\n---\n{raw[:800]}\n---")


def _extract_summary(stdout: str) -> dict:
    """Pull the validated object out of a `--output-format json` envelope.

    With --json-schema the result lands in `structured_output` (a dict); the
    `result` text stream is the agent's chatty completion and is unreliable.
    """
    env = json.loads(stdout)
    if env.get("is_error") or env.get("subtype") not in (None, "success"):
        raise RuntimeError(
            f"claude reported error (subtype={env.get('subtype')}): "
            f"{str(env.get('result'))[:300]}"
        )
    so = env.get("structured_output")
    if isinstance(so, dict) and so:
        return so
    result = env.get("result")
    if isinstance(result, dict) and result:
        return result
    if isinstance(result, str) and result.strip():
        return _parse_json_text(result)
    raise RuntimeError("claude envelope had no structured_output or result JSON")


def select_posts_for_llm(posts: list[dict], cap: int) -> list[dict]:
    """Top-`cap` posts by signal weight (pain-language first, then tier, then
    score). Anchors are stable, so the subset still resolves against the full
    appendix in the digest."""
    if len(posts) <= cap:
        return posts
    ranked = sorted(
        posts,
        key=lambda p: (
            0 if p["query_set"] == "pain_language" else 1,
            p["subreddit_tier"],
            -(p.get("score", 0) or 0),
        ),
    )
    return ranked[:cap]


def summarize_with_claude(pull: dict, timeout: int = 600) -> dict:
    claude_cmd = resolve_claude_cmd()
    # Send only the top-weighted subset for analysis (a full run can overflow the
    # call); keep the real header totals so coverage/confidence stays accurate.
    selected = select_posts_for_llm(pull["posts"], LLM_POST_CAP)
    llm_pull = dict(pull)
    llm_pull["posts"] = selected
    if len(selected) < len(pull["posts"]):
        llm_pull["_analysis_note"] = (
            f"posts truncated to top {len(selected)} of {len(pull['posts'])} by "
            "signal weight (pain-language + Tier 1 first); full set is in the digest."
        )
    # Full contract + pull JSON go through stdin to dodge the Windows .CMD
    # command-line length limit; only the schema and a short system string ride
    # on argv. --output-format json so we can read `structured_output`.
    user_prompt = (
        SUMMARY_SYSTEM_PROMPT
        + "\n\n=== PULL JSON TO ANALYZE (this is the user-message payload) ===\n"
        + build_summary_prompt(llm_pull)
    )
    args = [
        claude_cmd,
        "-p",
        "--no-session-persistence",
        "--system-prompt", SHORT_SYSTEM_PROMPT,
        "--json-schema", json.dumps(SUMMARY_SCHEMA),
        "--output-format", "json",
    ]
    last_err: Exception | None = None
    for attempt in range(1, LLM_RETRIES + 1):
        print(
            f"Calling claude -p (stdin ~{len(user_prompt)} chars, "
            f"attempt {attempt}/{LLM_RETRIES})...",
            flush=True,
        )
        try:
            proc = subprocess.run(
                args,
                input=user_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"claude CLI exited {proc.returncode}: "
                    f"{(proc.stderr or '').strip()[:500]!r}"
                )
            return _extract_summary(proc.stdout)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last_err = e
            print(f"  attempt {attempt} failed: {str(e)[:200]}", flush=True)
            if attempt < LLM_RETRIES:
                time.sleep(LLM_RETRY_SLEEP)
    raise RuntimeError(
        f"claude summarization failed after {LLM_RETRIES} attempts: {last_err}"
    )


# ============================================================================
# Notion block helpers
# ============================================================================
def rt(text: str, link: str | None = None, bold: bool = False) -> dict:
    obj: dict = {"type": "text", "text": {"content": text}}
    if link:
        obj["text"]["link"] = {"url": link}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


def heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": [rt(text)]}}


def paragraph(rich: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def bullet(rich: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich},
    }


def numbered(rich: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": rich},
    }


def callout(rich: list[dict], emoji: str = "📊") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {"rich_text": rich, "icon": {"type": "emoji", "emoji": emoji}},
    }


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def quote_block(rich: list[dict]) -> dict:
    return {"object": "block", "type": "quote", "quote": {"rich_text": rich}}


def ref_rich(anchor: str, by_anchor: dict[str, dict]) -> list[dict]:
    """Resolve an anchor like '#185' to clickable rich text."""
    key = str(anchor).lstrip("#").strip()
    p = by_anchor.get(key)
    if not p:
        return [rt(f"[#{key}] (unknown ref)")]
    title = (p["title"] or "(no title)")[:80]
    tag = "PAIN" if p["query_set"] == "pain_language" else "ai"
    meta = f"  · {p['subreddit']} · T{p['subreddit_tier']} · {tag} · score {p['score']}"
    parts = [rt(f"#{key} ", bold=True)]
    if p["url"]:
        parts.append(rt(title, link=p["url"]))
    else:
        parts.append(rt(title))
    parts.append(rt(meta))
    return parts


def _entries(rich_list: list[list[dict]], block_fn=bullet) -> list[dict]:
    return [block_fn(r) for r in rich_list]


# ============================================================================
# Section renderers — plain-language four-lens digest
# ============================================================================
def ref_compact(anchor: str, by_anchor: dict) -> list[dict]:
    """A short clickable reference: '#35 (r/smallbusiness)'."""
    key = str(anchor or "").lstrip("#").strip()
    p = by_anchor.get(key)
    if not p:
        return [rt(f"[#{key}]")] if key else []
    label = f"#{key} ({p['subreddit']})"
    return [rt(label, link=p["url"])] if p.get("url") else [rt(label)]


def _quote_with_attr(quote: str, anchor: str, by_anchor: dict) -> list[dict]:
    blocks = [quote_block([rt(f"“{quote.strip()}”")])]
    attr = ref_compact(anchor, by_anchor)
    if attr:
        blocks.append(paragraph([rt("— ")] + attr))
    return blocks


def build_gist(gist: str) -> list[dict]:
    g = (gist or "").strip()
    return [heading(2, "The gist"), paragraph([rt(g or "(no summary produced)")])]


def build_asks(items: list[dict], by_anchor: dict) -> list[dict]:
    blocks = [heading(2, "What people are asking for")]
    if not items:
        blocks.append(paragraph([rt("(nothing clearly surfaced this week)")]))
        return blocks
    for it in items:
        ask = (it.get("ask") or "").strip()
        meta = [s for s in [(it.get("who") or "").strip(),
                            (it.get("prevalence") or "").strip()] if s]
        line = [rt(ask, bold=True)]
        if meta:
            line.append(rt("  —  " + " · ".join(meta)))
        blocks.append(bullet(line))
        quote = (it.get("quote") or "").strip()
        if quote:
            blocks += _quote_with_attr(quote, it.get("anchor"), by_anchor)
    return blocks


def build_points(items: list[dict], by_anchor: dict, title: str, empty: str) -> list[dict]:
    blocks = [heading(2, title)]
    if not items:
        blocks.append(paragraph([rt(empty)]))
        return blocks
    for it in items:
        point = (it.get("point") or "").strip()
        why = (it.get("why") or "").strip()
        line = [rt(point, bold=True)]
        if why:
            line.append(rt(" — " + why))
        ref = ref_compact(it.get("anchor"), by_anchor)
        if ref:
            line += [rt("  · ")] + ref
        blocks.append(bullet(line))
    return blocks


def build_struggle(struggle: dict) -> list[dict]:
    blocks = [heading(2, "What they're really struggling with")]
    insight = (struggle.get("insight") or "").strip()
    means = (struggle.get("what_it_means_for_chorrus") or "").strip()
    blocks.append(paragraph([rt(insight or "(no insight produced)")]))
    if means:
        blocks.append(
            callout([rt("What this means for Chorrus: ", bold=True), rt(means)], emoji="💡")
        )
    return blocks


def build_people(items: list[dict], by_anchor: dict) -> list[dict]:
    blocks = [heading(2, "People worth messaging")]
    if not items:
        blocks.append(paragraph([rt("(none clearly worth reaching out to this week)")]))
        return blocks
    for it in items:
        who = (it.get("who") or "").strip()
        line = ref_compact(it.get("anchor"), by_anchor)
        if who:
            line += [rt("  —  ")] + [rt(who, bold=True)]
        blocks.append(bullet(line))
        need = (it.get("what_they_need") or "").strip()
        if need:
            blocks.append(paragraph([rt("    needs: ", bold=True), rt(need)]))
        quote = (it.get("quote") or "").strip()
        if quote:
            blocks.append(quote_block([rt(f"“{quote}”")]))
    return blocks


def build_appendix(posts: list[dict]) -> list[dict]:
    """All posts by anchor so every ref resolves and is auditable."""
    blocks = [heading(2, "Appendix — all posts by anchor")]
    for p in posts:
        tag = "PAIN" if p["query_set"] == "pain_language" else "ai"
        meta = (
            f"  · {p['subreddit']} · T{p['subreddit_tier']} · {tag}"
            f" · score {p['score']} · {p['num_comments']}c · {p['author']}"
        )
        if p["author_promotional_flag"]:
            meta += " · [PROMO]"
        title = p["title"] or "(no title)"
        rich = [rt(f"{p['anchor']} ", bold=True)]
        if p["url"]:
            rich.append(rt(title, link=p["url"]))
        else:
            rich.append(rt(title))
        rich.append(rt(meta))
        blocks.append(bullet(rich))
    return blocks


def build_blocks(pull: dict, errors: list[dict], summary: dict | None) -> list[dict]:
    posts = pull["posts"]
    by_anchor = {p["anchor"].lstrip("#"): p for p in posts}

    header = (
        f"{pull['total_posts']} posts · T1 {pull['tier1_posts']} / "
        f"T2 {pull['tier2_posts']} / T3 {pull['tier3_posts']} · "
        f"pain {pull['pain_language_posts']} / ai {pull['ai_language_posts']} · "
        f"{len(pull['subreddits_queried'])} subs · generated {pull['pull_date']}"
    )
    blocks: list[dict] = [callout([rt(header)])]

    if errors:
        err_text = f"Fetch errors on {len(errors)} (sub, query) pairs (see raw JSON)."
        blocks.append(callout([rt(err_text)], emoji="⚠️"))

    if summary and summary.get("input_invalid"):
        blocks.append(callout([rt(str(summary["input_invalid"]))], emoji="⛔"))
        summary = None

    if summary:
        blocks += build_gist(summary.get("gist") or "")
        blocks.append(divider())
        blocks += build_asks(summary.get("what_people_are_asking") or [], by_anchor)
        blocks.append(divider())
        blocks += build_points(
            summary.get("supports_chorrus") or [], by_anchor,
            "What supports Chorrus", "(no clear support this week)",
        )
        blocks.append(divider())
        blocks += build_points(
            summary.get("works_against_chorrus") or [], by_anchor,
            "What works against Chorrus", "(no clear headwinds this week)",
        )
        blocks.append(divider())
        blocks += build_struggle(summary.get("real_struggle") or {})
        blocks.append(divider())
        blocks += build_people(summary.get("people_to_message") or [], by_anchor)
        blocks.append(divider())

    blocks += build_appendix(posts)
    return blocks


def create_notion_page(token: str, parent_id: str, title: str, blocks: list[dict]) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "parent": {"page_id": parent_id},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        "children": blocks[:100],
    }
    resp = requests.post(NOTION_API, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Notion API {resp.status_code}: {resp.text}")
    page = resp.json()
    page_id = page["id"]

    remaining = blocks[100:]
    while remaining:
        chunk, remaining = remaining[:100], remaining[100:]
        ar = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": chunk},
            timeout=30,
        )
        if ar.status_code >= 400:
            raise RuntimeError(f"Notion append {ar.status_code}: {ar.text}")
    return page["url"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=25, help="results per (sub, query), max 100")
    ap.add_argument("--max-subs", type=int, default=None, help="test: only first N subreddits")
    ap.add_argument("--subs", default=None, help="test: comma-separated subreddit names to restrict to")
    ap.add_argument(
        "--sleep", type=float, default=SLEEP_BETWEEN_QUERIES,
        help=f"base seconds between requests (default {SLEEP_BETWEEN_QUERIES}; jitter added on top)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print pull stats, skip Notion")
    ap.add_argument(
        "--from-pull", default=None, metavar="PATH",
        help="skip scraping; load an existing reddit_digest_<date>.json and just "
             "(re)summarize + publish. Pair with --refresh-summary to force a fresh LLM call.",
    )
    ap.add_argument("--no-summary", action="store_true", help="skip LLM analysis")
    ap.add_argument(
        "--refresh-summary", action="store_true",
        help="force a fresh LLM call even if today's summary file already exists",
    )
    ap.add_argument("--no-bodies", action="store_true", help="skip fetching post bodies")
    ap.add_argument(
        "--body-min", type=int, default=0,
        help="only fetch bodies for posts with score>=N or comments>=N (Tier 1 always; 0=all)",
    )
    args = ap.parse_args()

    load_dotenv()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Reddit digest for {today}", flush=True)

    script_dir = Path(__file__).resolve().parent

    if args.from_pull:
        # Re-summarize an existing pull without re-scraping Reddit.
        pull_path = Path(args.from_pull)
        if not pull_path.is_absolute():
            pull_path = script_dir / pull_path
        pull = json.loads(pull_path.read_text(encoding="utf-8"))
        errors = pull.pop("errors", []) if isinstance(pull, dict) else []
        posts = pull.get("posts", [])
        today = pull.get("pull_date", today)
        print(
            f"Loaded pull from {pull_path.name}: {len(posts)} posts (date {today}). "
            "Skipping scrape.",
            flush=True,
        )
    else:
        subs_filter = [s.strip() for s in args.subs.split(",")] if args.subs else None
        raw_by_id, errors, subs_queried = fetch_all(
            args.limit, args.max_subs, args.sleep, subs_filter
        )
        kept = filter_posts(raw_by_id)
        if not args.no_bodies:
            fetch_bodies(kept, args.sleep, args.body_min)
        posts = finalize_posts(kept)
        pull = build_pull(posts, subs_queried, today)
        if _throttle_state["events"]:
            print(
                f"Throttle events: {_throttle_state['events']} "
                f"(adaptive delay settled at +{_throttle_state['extra']:.0f}s/request)",
                flush=True,
            )
        print(
            f"Pull: {pull['total_posts']} posts "
            f"(T1 {pull['tier1_posts']} / T2 {pull['tier2_posts']} / T3 {pull['tier3_posts']}; "
            f"pain {pull['pain_language_posts']} / ai {pull['ai_language_posts']})",
            flush=True,
        )
        raw_path = script_dir / f"reddit_digest_{today}.json"
        raw_path.write_text(
            json.dumps({"errors": errors, **pull}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Saved pull JSON -> {raw_path}", flush=True)

    if not posts and not args.dry_run:
        print("No posts fetched — aborting Notion write.", file=sys.stderr)
        return 1

    summary: dict | None = None
    summary_failed = False
    summary_path = script_dir / f"reddit_summary_{today}.json"
    if not args.no_summary and posts:
        # Reuse a summary already produced for today (e.g. by a prior run or a
        # manual re-run) so one flaky `claude` call doesn't cost us the analysis.
        # `--refresh-summary` forces a fresh call even when the file exists.
        if summary_path.exists() and not args.refresh_summary:
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                print(f"Reusing existing summary <- {summary_path}", flush=True)
            except (OSError, json.JSONDecodeError) as e:
                print(f"WARNING: could not read {summary_path}: {e}", file=sys.stderr)
        if summary is None:
            try:
                summary = summarize_with_claude(pull)
                summary_path.write_text(
                    json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"Saved summary -> {summary_path}", flush=True)
            except Exception as e:
                summary_failed = True
                print(f"ERROR: summarization failed: {e}", file=sys.stderr)
                print(
                    "Writing digest with appendix only. Re-run "
                    "(or `python reddit_digest.py --refresh-summary`) once "
                    f"`claude` is reachable to backfill {summary_path.name}.",
                    file=sys.stderr,
                )

    if args.dry_run:
        print("\n--- Top Tier-1 / pain-language posts (dry run) ---")
        hi = [p for p in posts if p["subreddit_tier"] == 1 and p["query_set"] == "pain_language"]
        for p in (hi or posts)[:15]:
            print(f"  {p['anchor']:>5} [{p['score']:>4}] {p['subreddit']:<22} {p['title'][:70]}")
        return 0

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("NOTION_TOKEN env var is not set.", file=sys.stderr)
        return 2
    parent_id = os.environ.get("NOTION_PARENT_PAGE_ID", PARENT_PAGE_ID)

    blocks = build_blocks(pull, errors, summary)
    print(f"Built {len(blocks)} Notion blocks", flush=True)
    url = create_notion_page(token, parent_id, f"{today} — Chorrus buyer-signal digest", blocks)
    print(f"Created Notion page: {url}", flush=True)
    if summary_failed:
        # Digest was published, but without the AI analysis — surface a non-zero
        # exit so the scheduler/log flags it instead of looking like a clean run.
        print("Digest published WITHOUT AI summary (see error above).", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
