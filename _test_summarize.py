import json
import reddit_digest as R

pull = json.load(open("reddit_digest_2026-05-29.json", encoding="utf-8"))
flag = "?"
try:
    s = R.summarize_with_claude(pull)
    json.dump(s, open("reddit_summary_2026-05-29.json", "w", encoding="utf-8"),
              indent=2, ensure_ascii=False)
    req = ["coverage_and_confidence", "bear_case", "concept_test_scoreboard",
           "true_buyer_handraisers", "competitor_watch", "atmospheric_risk",
           "recurring_vs_emerging", "notable_quotes", "what_to_do_this_week"]
    missing = [k for k in req if k not in s]
    conf = (s.get("coverage_and_confidence") or {}).get("confidence", "?")
    flag = "OK keys=%d missing=%d conf=%s bear=%d handraisers=%d quotes=%d comp=%d" % (
        len(s), len(missing), conf, len(s.get("bear_case", [])),
        len(s.get("true_buyer_handraisers", [])), len(s.get("notable_quotes", [])),
        len(s.get("competitor_watch", [])))
except Exception as e:
    flag = "FAIL %s %s" % (type(e).__name__, str(e)[:160])
open("_summ.flag", "w", encoding="ascii", errors="replace").write(flag)
