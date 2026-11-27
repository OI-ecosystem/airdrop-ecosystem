#!/usr/bin/env python3
"""Hunter Intelligence — weekly newsletter.

Event-led, not score-led. "Morpho moved two points" is not news; "Extended's
window closes in four days and Backpack has distributed" is. The event log
supplies the spine, the archive supplies the evidence, and the AI pass adds
prose over the top of facts that are already true without it.

The data spine is built first and stands alone. If the AI call fails, the
newsletter still goes out — thinner, but correct. That ordering is
deliberate: the facts must not depend on the model being available.

Published on a seven-day delay to the public site; the current issue is for
subscribers. The delay is the gate and the demonstration at once — last
week's issue read beside this week's table lets a reader check the calls
rather than take them on trust.

Usage:
    python3 build_newsletter.py                 # build current issue
    python3 build_newsletter.py --dry-run       # build, print, don't publish
    python3 build_newsletter.py --weeks-ago 1   # rebuild a past issue
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ARCHIVE = APP_DIR / "briefings"
SCORES = APP_DIR / "hunter_scores.json"
OUT_DIR = APP_DIR / "newsletters"

# Publishing to the proxy is OFF until a route exists to receive it.
# The first version POSTed to /api/scores?view=newsletter — a route that
# was never built; scores.js handles only view=trajectory. A client
# written against an imagined server produced a 401 in every run and
# made a healthy pipeline look broken. The issue does not need a push
# anyway: hunter_app attaches the week-old issue from newsletters/ to
# the scan payload, which is how the site serves it. Set
# newsletter_endpoint in hunter_config.json if and when a real consumer
# for the CURRENT issue exists (e.g. the subscriber mailer).
NEWSLETTER_ENDPOINT = None
AI_ENDPOINT = "https://rcloop-proxy.vercel.app/api/ai"
def _config():
    """Read hunter_config.json, the same file hunter_app.py reads.

    This previously read HUNTER_KEY from the environment — a convention
    that exists nowhere else in this codebase and was never set, so the
    publish step sent an empty key and the proxy answered 401. One
    module inventing its own way to find a credential is how that
    happens; both now read the same file.
    """
    try:
        f = APP_DIR / "hunter_config.json"
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}

CONFIG = _config()
HUNTER_KEY = CONFIG.get("hunter_key") or os.environ.get("HUNTER_KEY", "")
AI_ENABLED = CONFIG.get("ai_enabled", True)

# Events worth leading on, most consequential first. Anything not listed
# is supporting detail rather than a headline.
LEAD_PRIORITY = [
    # A claim deadline for holders outranks everything: it is the only
    # kind of event where days matter and money is forfeited by inaction.
    # Without it in this list the lead fell through to whatever came
    # next — one issue led with "Superposition is no longer tracked"
    # while Extended's signing cutoff sat three sections down.
    "action_required",
    "distributed",
    "deadline_near",
    "candidate_cleared",
    "conviction_change",
    "deadline_set",
    "tvl_surge",
    "entered_universe",
    "score_shift",
    "deadline_passed",
    "left_universe",
]

KIND_LABEL = {
    "distributed":       "Distributed",
    "deadline_near":     "Closing soon",
    "action_required":   "Act now",
    "left_universe":     "No longer tracked",
    "entered_universe":  "Now tracked",
    "deadline_set":      "Date confirmed",
    "deadline_passed":   "Window closed",
    "entered_universe":  "Now tracking",
    "left_universe":     "No longer tracked",
    "candidate_found":   "Spotted",
    "candidate_cleared": "Cleared quarantine",
    "score_shift":       "Score moved",
    "tvl_surge":         "TVL moved",
    "conviction_change": "Conviction changed",
}


def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  [{level}]  {msg}", flush=True)


# ── Data spine ─────────────────────────────────────────────────────────

def load_scan(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def archive_files(since=None, until=None):
    out = []
    for f in sorted(ARCHIVE.glob("*.json")):
        stem = f.stem
        if since and stem < since:
            continue
        if until and stem > until:
            continue
        out.append(f)
    return out


def gather(weeks_ago=0):
    """Assemble everything the issue needs. Returns a dict, no prose."""
    now = datetime.now(timezone.utc) - timedelta(weeks=weeks_ago)
    week_start = now - timedelta(days=7)
    since = week_start.strftime("%Y-%m-%d")
    until = now.strftime("%Y-%m-%d-%H%M")

    # Events for the period.
    #
    # Filtered on when the thing HAPPENED, not when we noticed it. The
    # five confirmed distributions were seeded today but occurred between
    # November 2024 and March 2026 — sorting on detection put "Backpack
    # has distributed" at the top of this week's issue eight months after
    # the fact. What we learned recently is not the same as what happened
    # recently, and only the second is news.
    events = []
    cutoff = week_start.strftime("%Y-%m-%d")
    try:
        from hunter_events import EventLog
        log_db = EventLog()
        today = now.strftime("%Y-%m-%d")
        for e in log_db.recent(days=90 + weeks_ago * 7, limit=500):
            when = (e.get("occurred_at") or e.get("detected_at") or "")[:10]
            detected = (e.get("detected_at") or "")[:10]
            if not when:
                continue
            # A future date is a deadline we are warning about, which is
            # the most actionable thing in the issue — keep it, provided
            # we noticed it recently. Past events must fall in the week.
            if when > today:
                if detected < cutoff:
                    continue
            elif when < cutoff:
                continue
            events.append(e)
        # Imminent deadlines first, then most recent
        events.sort(key=lambda x: (
            0 if (x.get("occurred_at") or "")[:10] > today else 1,
            (x.get("occurred_at") or "")[:10] if (x.get("occurred_at") or "")[:10] > today
            else "",
            x.get("detected_at") or ""), reverse=False)
        events = ([e for e in events if (e.get("occurred_at") or "")[:10] > today]
                  + sorted([e for e in events if (e.get("occurred_at") or "")[:10] <= today],
                           key=lambda x: x.get("occurred_at") or x["detected_at"],
                           reverse=True))
    except Exception as e:
        log(f"Event log unavailable: {type(e).__name__}: {e}", "WARN")

    # Current standings
    current = load_scan(SCORES)
    if weeks_ago:
        files = archive_files(until=until)
        if files:
            current = load_scan(files[-1])

    protocols = (current or {}).get("protocols", [])

    # Score movement across the week, from the archive, on complete records
    files = archive_files(since=since, until=until)
    first, last = (load_scan(files[0]) if files else None,
                   load_scan(files[-1]) if files else None)
    movement = []
    if first and last:
        a = {p.get("id") or p["name"]: p for p in first.get("protocols", [])}
        b = {p.get("id") or p["name"]: p for p in last.get("protocols", [])}
        same_model = (first.get("scoring_version") == last.get("scoring_version"))
        for k, pb in b.items():
            pa = a.get(k)
            if not pa or not same_model:
                continue
            d = (pb.get("score") or 0) - (pa.get("score") or 0)
            if d:
                movement.append({"name": pb["name"], "delta": d,
                                 "score": pb.get("score"),
                                 "confidence": pb.get("confidence", 0)})
        movement.sort(key=lambda x: -abs(x["delta"]))

    # Deadlines inside the next fortnight
    upcoming = sorted(
        [p for p in protocols
         if p.get("days_remaining") is not None and 0 <= p["days_remaining"] <= 14],
        key=lambda p: p["days_remaining"])

    by_kind = defaultdict(list)
    for e in events:
        by_kind[e["kind"]].append(e)

    lead = None
    for kind in LEAD_PRIORITY:
        if by_kind.get(kind):
            lead = by_kind[kind][0]
            break

    measured = [p for p in protocols if (p.get("confidence") or 0) > 0]

    return {
        "issue_date":    now.strftime("%Y-%m-%d"),
        "period_start":  week_start.strftime("%Y-%m-%d"),
        "period_end":    now.strftime("%Y-%m-%d"),
        "scans":         len(files),
        "lead":          lead,
        "events":        events,
        "events_by_kind": dict(by_kind),
        "top":           protocols[:5],
        "upcoming":      upcoming,
        "movement":      movement[:5],
        "tracked":       len(protocols),
        "measured":      len(measured),
        "scoring_version": (current or {}).get("scoring_version")
                           or (protocols[0].get("scoring_version") if protocols else None),
        "model_changed": bool(first and last and
                              first.get("scoring_version") != last.get("scoring_version")),
    }


# ── Rendering ──────────────────────────────────────────────────────────

def render_markdown(d, prose=None):
    L = []
    L.append(f"# Hunter Intelligence — week to {d['issue_date']}")
    L.append("")

    if prose:
        L.append(prose.strip())
        L.append("")

    if d["lead"]:
        # A deadline event's headline was written when it was detected, so
        # it ages by a day for every scan. Recompute from the live figure
        # rather than printing "4 days" above a section that says 3.
        headline = d["lead"]["headline"]
        if d["lead"]["kind"] == "deadline_near":
            live = next((p for p in d["upcoming"]
                         if p["name"] == d["lead"].get("protocol_name")), None)
            if live:
                n = live["days_remaining"]
                when = "today" if n == 0 else f"in {n} day{'s' if n != 1 else ''}"
                headline = f"{live['name']} window closes {when}"
        L.append(f"**{headline}**")
        if d["lead"].get("detail"):
            L.append("")
            L.append(d["lead"]["detail"])
        L.append("")

    if d["upcoming"]:
        L.append("## Closing soon")
        L.append("")
        for p in d["upcoming"]:
            days = p["days_remaining"]
            when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
            # Same distinction the event log draws: a date on a protocol
            # whose entry has closed is a claim deadline for existing
            # holders, not a window a reader can still get into.
            if p.get("entry_window") == "closed":
                L.append(f"- **{p['name']}** — claim deadline {when}"
                         f" ({p.get('deadline_iso') or p.get('deadline')})."
                         f" Entry closed; applies only if you already hold an allocation.")
            else:
                L.append(f"- **{p['name']}** — {when}"
                         f" ({p.get('deadline_iso') or p.get('deadline')})."
                         f" Score {p.get('score')}.")
        L.append("")

    interesting = [e for e in d["events"]
                   if e["kind"] in ("distributed", "candidate_cleared",
                                    "entered_universe", "left_universe",
                                    "conviction_change", "tvl_surge")]
    if interesting:
        L.append("## What changed")
        L.append("")
        for e in interesting[:12]:
            L.append(f"- **{KIND_LABEL.get(e['kind'], e['kind'])}** — {e['headline']}")
        L.append("")

    if d["movement"] and not d["model_changed"]:
        L.append("## Score movement")
        L.append("")
        for m in d["movement"]:
            L.append(f"- {m['name']} {m['delta']:+d} to {m['score']}"
                     f" (confidence {m['confidence']:.2f})")
        L.append("")
    elif d["model_changed"]:
        L.append("## Score movement")
        L.append("")
        L.append("_The scoring model changed during this period, so movements "
                 "are not comparable and have been withheld rather than "
                 "presented as market activity._")
        L.append("")

    if d["top"]:
        L.append("## Standing")
        L.append("")
        L.append("| # | Protocol | Score | Evidence |")
        L.append("|---|---|---|---|")
        for p in d["top"]:
            c = p.get("confidence") or 0
            band = "Measured" if c >= 0.4 else ("Partial" if c > 0 else "Unvalidated")
            # Unranked protocols carry rank None on purpose; printing
            # Python's None into a table is not the same as saying so.
            rk = p.get('rank') or "\u2014"
            L.append(f"| {rk} | {p['name']} | {p.get('score')} | {band} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append(f"_{d['scans']} scans between {d['period_start']} and {d['period_end']}. "
             f"{d['tracked']} protocols tracked, {d['measured']} with at least one "
             f"live measured factor. Scoring model v{d['scoring_version']}._")
    L.append("")
    L.append("_Scores combine six factors. Two to three are measured live from "
             "on-chain data; the rest are hand-assessed and change rarely. The "
             "evidence column says which is which — we would rather show you a "
             "thin score than a confident one._")

    return "\n".join(L)


# ── AI pass ────────────────────────────────────────────────────────────

AI_BRIEF = """You are writing the opening two paragraphs of a weekly airdrop
intelligence newsletter for experienced crypto farmers.

Facts for this week, which are the ONLY things you may assert:
{facts}

Rules:
- Two short paragraphs, no heading, no bullet points.
- Lead with whatever genuinely matters most to someone deciding where to
  spend gas this week.
- State nothing not present in the facts above. Do not infer prices,
  valuations, team quality or likelihood of a drop.
- If the week was quiet, say so plainly. A dull week reported honestly is
  worth more than a manufactured one.
- No hype, no exclamation marks, no "the crypto space". Write as one
  trader to another.
"""


def ai_prose(d):
    try:
        import requests
    except ImportError:
        return None

    facts = []
    if d["lead"]:
        facts.append(f"- Lead: {d['lead']['headline']}. {d['lead'].get('detail') or ''}")
    for p in d["upcoming"]:
        facts.append(f"- {p['name']} window closes in {p['days_remaining']} days")
    for e in d["events"][:10]:
        facts.append(f"- {e['headline']}")
    for m in d["movement"][:3]:
        facts.append(f"- {m['name']} score {m['delta']:+d} to {m['score']}")
    facts.append(f"- {d['tracked']} protocols tracked, {d['measured']} with live measured data")
    if not d["events"]:
        facts.append("- No material events recorded this week")

    try:
        r = requests.post(AI_ENDPOINT,
                          json={"prompt": AI_BRIEF.format(facts="\n".join(facts))},
                          headers={"Content-Type": "application/json"},
                          timeout=40)
        if r.status_code != 200:
            log(f"AI pass returned {r.status_code}", "WARN")
            return None
        body = r.json()
        return _extract_text(body)
    except Exception as e:
        # Log the message, not just the class. "AttributeError" alone
        # cost a round trip to diagnose; the message names the field.
        log(f"AI pass failed, publishing data-only: {type(e).__name__}: {e}", "WARN")
        return None


def _extract_text(body):
    """Pull the text out of whatever shape the proxy returns.

    The endpoint answers in Anthropic's format, where content is a list
    of blocks: {"content": [{"type":"text","text":"..."}]}. Treating it
    as a string is what raised AttributeError — a list is truthy, so it
    passed straight through an `or` chain and into .strip().
    """
    if body is None:
        return None
    if isinstance(body, str):
        return body.strip() or None
    if isinstance(body, list):
        parts = [_extract_text(b) for b in body]
        joined = "\n".join(x for x in parts if x)
        return joined.strip() or None
    if isinstance(body, dict):
        blocks = body.get("content")
        if isinstance(blocks, list):
            return _extract_text(blocks)
        for k in ("text", "completion", "message", "output"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (list, dict)):
                got = _extract_text(v)
                if got:
                    return got
    return None


def publish(payload):
    endpoint = CONFIG.get("newsletter_endpoint") or NEWSLETTER_ENDPOINT
    if not endpoint:
        log("[Newsletter] no publish endpoint configured — issue saved "
            "locally; the site receives it via the next scan's payload")
        return
    try:
        import requests
    except ImportError:
        log("requests unavailable — not published", "WARN")
        return
    try:
        r = requests.post(endpoint, json=payload,
                          headers={"Content-Type": "application/json",
                                   "X-Hunter-Key": HUNTER_KEY},
                          timeout=20)
        log(f"[Newsletter] {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log(f"[Newsletter] publish failed: {type(e).__name__}: {e}", "ERROR")


def main():
    dry = "--dry-run" in sys.argv
    weeks = 0
    if "--weeks-ago" in sys.argv:
        weeks = int(sys.argv[sys.argv.index("--weeks-ago") + 1])
    skip_ai = "--no-ai" in sys.argv

    d = gather(weeks_ago=weeks)
    log(f"{len(d['events'])} events, {d['scans']} scans, "
        f"{len(d['upcoming'])} deadline(s) inside a fortnight")

    prose = None if (dry and skip_ai) or skip_ai else ai_prose(d)
    md = render_markdown(d, prose)

    OUT_DIR.mkdir(exist_ok=True)
    stem = f"hunter-weekly-{d['issue_date']}"
    (OUT_DIR / f"{stem}.md").write_text(md)
    (OUT_DIR / f"{stem}.json").write_text(json.dumps(
        {"issue_date": d["issue_date"], "markdown": md,
         "has_prose": bool(prose), "data": {
             k: v for k, v in d.items() if k != "events_by_kind"}}, indent=2, default=str))
    log(f"Written {OUT_DIR / (stem + '.md')}")

    if dry:
        print("\n" + "=" * 72 + "\n")
        print(md)
        print("\n" + "=" * 72)
    else:
        publish({"issue_date": d["issue_date"], "markdown": md,
                 "generated_at": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    main()
