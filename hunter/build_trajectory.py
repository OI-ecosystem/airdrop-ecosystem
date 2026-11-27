#!/usr/bin/env python3
"""Hunter Intelligence — trajectory engine.

Reads the whole briefings/ archive and derives what a single scan cannot
show: how each protocol's conviction has moved, how long it has held,
what changed this week, and when it was first and last seen.

Nothing here is new capture. Every number comes from scans already on
disk — which is the point: a competitor can copy a feature in a sprint,
but not three months of consistent scoring.

Writes hunter_trajectory.json and publishes it to the Vercel route so
the site can render it.

Usage:
    python3 build_trajectory.py            # build and publish
    python3 build_trajectory.py --dry-run  # build, print summary, no push
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ARCHIVE = APP_DIR / "briefings"
OUTFILE = APP_DIR / "hunter_trajectory.json"

TRAJECTORY_ENDPOINT = "https://rcloop-proxy.vercel.app/api/scores?view=trajectory"
HUNTER_KEY = os.environ.get("HUNTER_KEY", "")

SUPPRESS = "SUPPRESS"
DAILY_POINTS = 90          # days of daily history to publish per protocol
MOVER_WINDOW_DAYS = 7
MOVER_COUNT = 5


def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  [{level}]  {msg}", flush=True)


# ── Load ────────────────────────────────────────────────────────────────

def load_scans():
    """Return [(datetime, scan_id, data)] sorted oldest first.

    Skips unreadable files and pre-2.0 records with no protocols array
    rather than failing the run — an archive this long will have the odd
    partial file and one bad scan should not cost us the other 390.
    """
    scans, skipped = [], 0
    for f in sorted(ARCHIVE.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            skipped += 1
            continue
        if not data.get("protocols"):
            skipped += 1
            continue
        try:
            dt = datetime.strptime(f.stem[:15], "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            skipped += 1
            continue
        scans.append((dt, data.get("scan_id", f.stem), data))

    scans.sort(key=lambda x: x[0])
    log(f"Loaded {len(scans)} scans ({skipped} skipped)")
    if scans:
        log(f"Range: {scans[0][0]:%Y-%m-%d} to {scans[-1][0]:%Y-%m-%d}")
    return scans


# ── Derive ──────────────────────────────────────────────────────────────

def build(scans):
    if not scans:
        raise SystemExit("No usable scans in archive — nothing to build.")

    history = defaultdict(list)
    for idx, (dt, scan_id, data) in enumerate(scans):
        for p in data.get("protocols", []):
            name = p.get("name")
            if not name:
                continue
            history[name].append({
                "i":          idx,
                "dt":         dt,
                "score":      p.get("score"),
                "rank":       p.get("rank"),
                "conviction": p.get("conviction"),
                "urgent":     bool(p.get("urgent")),
            })

    latest_idx = len(scans) - 1
    now = scans[-1][0]
    protocols = {}

    for name, appearances in history.items():
        appearances.sort(key=lambda a: a["i"])
        scores = [a["score"] for a in appearances if a["score"] is not None]
        if not scores:
            continue

        in_latest = appearances[-1]["i"] == latest_idx

        # Consecutive run of scans ending at the most recent appearance
        consecutive = 1
        for j in range(len(appearances) - 1, 0, -1):
            if appearances[j]["i"] - appearances[j - 1]["i"] == 1:
                consecutive += 1
            else:
                break

        # Consecutive scans held above the suppression threshold
        above_run = 0
        for a in reversed(appearances):
            if a["conviction"] and a["conviction"] != SUPPRESS:
                above_run += 1
            else:
                break

        current = appearances[-1]["score"]

        def score_at(days_ago):
            """Score at the scan closest to N days before the latest."""
            target = now.timestamp() - days_ago * 86400
            best, gap = None, None
            for a in appearances:
                g = abs(a["dt"].timestamp() - target)
                if gap is None or g < gap:
                    best, gap = a, g
            # Only meaningful if we found a scan within 36h of the target
            if best is None or gap > 36 * 3600:
                return None
            return best["score"]

        wk = score_at(7)
        mo = score_at(30)

        # One point per day — the closing score of each day
        daily = {}
        for a in appearances:
            daily[a["dt"].strftime("%Y-%m-%d")] = a["score"]
        daily_series = [{"d": d, "s": s} for d, s in sorted(daily.items())][-DAILY_POINTS:]

        if in_latest and len(appearances) == 1:
            status = "NEW"
        elif not in_latest:
            status = "DISAPPEARED"
        elif len(scores) > 1 and scores[-1] != scores[-2]:
            status = "CHANGED"
        else:
            status = "PERSISTING"

        protocols[name] = {
            "name":              name,
            "status":            status,
            "current_score":     current,
            "current_rank":      appearances[-1]["rank"],
            "current_conviction": appearances[-1]["conviction"],
            "change_7d":         (current - wk) if (wk is not None and current is not None) else None,
            "change_30d":        (current - mo) if (mo is not None and current is not None) else None,
            "high":              max(scores),
            "low":               min(scores),
            "mean":              round(sum(scores) / len(scores), 1),
            "scans_observed":    len(appearances),
            "consecutive_scans": consecutive,
            "scans_above_threshold": above_run,
            "days_tracked":      (appearances[-1]["dt"] - appearances[0]["dt"]).days + 1,
            "first_seen":        appearances[0]["dt"].strftime("%Y-%m-%d"),
            "last_seen":         appearances[-1]["dt"].strftime("%Y-%m-%d"),
            "daily":             daily_series,
        }

    # Weekly movers — only where a genuine 7-day comparison exists
    movable = [p for p in protocols.values() if p["change_7d"] is not None]
    climbers = sorted(movable, key=lambda p: -p["change_7d"])[:MOVER_COUNT]
    fallers = sorted(movable, key=lambda p: p["change_7d"])[:MOVER_COUNT]

    return {
        "generated_at":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "archive": {
            "scans":        len(scans),
            "first_scan":   scans[0][0].strftime("%Y-%m-%d"),
            "last_scan":    scans[-1][0].strftime("%Y-%m-%d"),
            "days_covered": (scans[-1][0] - scans[0][0]).days + 1,
        },
        "movers": {
            "window_days": MOVER_WINDOW_DAYS,
            "climbers": [{"name": p["name"], "change": p["change_7d"], "score": p["current_score"]}
                         for p in climbers if p["change_7d"] > 0],
            "fallers":  [{"name": p["name"], "change": p["change_7d"], "score": p["current_score"]}
                         for p in fallers if p["change_7d"] < 0],
        },
        "protocols": protocols,
    }


# ── Publish ─────────────────────────────────────────────────────────────

def publish(payload):
    try:
        import requests
    except ImportError:
        log("requests not available — skipping push", "WARN")
        return
    log(f"[Trajectory] Pushing to {TRAJECTORY_ENDPOINT}")
    try:
        r = requests.post(
            TRAJECTORY_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json", "X-Hunter-Key": HUNTER_KEY},
            timeout=20,
        )
        if r.status_code == 200:
            res = r.json()
            log(f"[Trajectory] SUCCESS — stored at {res.get('stored_at','?')} "
                f"({res.get('count','?')} protocols, {res.get('kept_in','?')})")
        else:
            log(f"[Trajectory] Push returned {r.status_code}: {r.text[:200]}", "WARN")
    except Exception as e:
        log(f"[Trajectory] Push failed: {type(e).__name__}: {e}", "ERROR")


def main():
    dry = "--dry-run" in sys.argv

    if not ARCHIVE.is_dir():
        raise SystemExit(f"Archive not found: {ARCHIVE}")

    payload = build(load_scans())
    OUTFILE.write_text(json.dumps(payload, indent=2))
    log(f"Written {OUTFILE} ({OUTFILE.stat().st_size // 1024} KB)")

    a = payload["archive"]
    log(f"Archive: {a['scans']} scans over {a['days_covered']} days "
        f"({a['first_scan']} to {a['last_scan']})")
    log(f"Protocols tracked: {len(payload['protocols'])}")

    top = sorted(payload["protocols"].values(),
                 key=lambda p: -(p["scans_above_threshold"]))[:5]
    log("Longest runs above threshold:")
    for p in top:
        log(f"    {p['name']:<16} {p['scans_above_threshold']:>4} scans   "
            f"score {p['current_score']}  7d {p['change_7d']:+d}"
            if p["change_7d"] is not None else
            f"    {p['name']:<16} {p['scans_above_threshold']:>4} scans   score {p['current_score']}")

    m = payload["movers"]
    if m["climbers"]:
        log("Climbers (7d): " + ", ".join(f"{c['name']} +{c['change']}" for c in m["climbers"]))
    if m["fallers"]:
        log("Fallers  (7d): " + ", ".join(f"{c['name']} {c['change']}" for c in m["fallers"]))

    if dry:
        log("Dry run — not published.")
    else:
        publish(payload)


if __name__ == "__main__":
    main()
