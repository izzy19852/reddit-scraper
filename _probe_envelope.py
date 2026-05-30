"""Fast probe: what does `claude -p --json-schema --output-format json` return,
and where does the structured object land? Tiny input so it's quick.
Writes findings to _probe.out (safe ascii only)."""
import json
import subprocess

import reddit_digest as R

claude = R.resolve_claude_cmd()
pull = json.load(open("reddit_digest_2026-05-29.json", encoding="utf-8"))
small = dict(pull)
small["posts"] = pull["posts"][:3]
small["total_posts"] = 3
user = R.SUMMARY_SYSTEM_PROMPT + "\n\n=== PULL JSON ===\n" + R.build_summary_prompt(small)

args = [
    claude, "-p", "--no-session-persistence",
    "--system-prompt", R.SHORT_SYSTEM_PROMPT,
    "--json-schema", json.dumps(R.SUMMARY_SCHEMA),
    "--output-format", "json",
]
notes = []
try:
    p = subprocess.run(args, input=user, capture_output=True, text=True,
                       encoding="utf-8", timeout=240)
    notes.append("exit=%d stdout_len=%d" % (p.returncode, len(p.stdout or "")))
    try:
        env = json.loads(p.stdout)
        notes.append("envelope_keys=" + ",".join(sorted(env.keys())))
        for k in ("type", "subtype", "is_error"):
            if k in env:
                notes.append("%s=%s" % (k, env[k]))
        res = env.get("result")
        notes.append("result_type=%s" % type(res).__name__)
        if isinstance(res, str):
            notes.append("result_len=%d result_head=%s" % (len(res), repr(res[:80])))
            try:
                inner = json.loads(res)
                notes.append("result_parses_json=YES keys=%d" % len(inner))
            except Exception:
                notes.append("result_parses_json=NO")
        elif isinstance(res, dict):
            notes.append("result_is_dict keys=%d" % len(res))
        # check for other plausible fields
        for k in ("structured_output", "structuredOutput", "output", "content"):
            if k in env:
                notes.append("HAS_FIELD:%s type=%s" % (k, type(env[k]).__name__))
    except Exception as e:
        notes.append("envelope_parse_FAIL=%s" % type(e).__name__)
        notes.append("raw_head=" + repr((p.stdout or "")[:120]))
except Exception as e:
    notes.append("EXC=%s" % type(e).__name__)

with open("_probe.out", "w", encoding="ascii", errors="replace") as f:
    f.write("\n".join(notes) + "\n")
