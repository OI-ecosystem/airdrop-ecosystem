#!/usr/bin/env python3
"""Hunter Intelligence — event intelligence.

Scores answer "which opportunity is strongest". Events answer "what just
happened". They are different products and conflating them produces a
newsletter that says "Morpho moved two points", which nobody wants to
read.

Hunter already generates events — protocols retire, deadlines pass,
candidates clear quarantine — but it has only ever logged them to
journald, where they scroll away. This records them as first-class,
immutable, queryable facts.

Every event carries a source and a verification state. Nothing in here
feeds a score: an event is an observation, and the route from
observation to score runs through evidence, not through this table.
That boundary is deliberate and should stay.

Usage:
    from hunter_events import EventLog, detect_events

    log = EventLog()
    detect_events(log, previous_scan, current_scan)
    log.recent(days=7)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
EVENT_DB = APP_DIR / "hunter_events.db"

# Event kinds. Deliberately coarse — a long tail of one-off types makes
# the log unqueryable and the newsletter unwritable.
DISTRIBUTED        = "distributed"          # token launched, window closed
DEADLINE_SET       = "deadline_set"         # a real date became known
DEADLINE_NEAR      = "deadline_near"        # inside the alert window
DEADLINE_PASSED    = "deadline_passed"      # window closed
ENTERED_UNIVERSE   = "entered_universe"     # newly tracked
LEFT_UNIVERSE      = "left_universe"        # no longer tracked
CANDIDATE_FOUND    = "candidate_found"      # discovery, in quarantine
CANDIDATE_CLEARED  = "candidate_cleared"    # survived quarantine
SCORE_SHIFT        = "score_shift"          # material move only
TVL_SURGE          = "tvl_surge"            # material TVL move
CONVICTION_CHANGE  = "conviction_change"    # crossed a threshold

# Thresholds. A log full of noise is worse than no log: if every ±1
# oscillation becomes an event, the material ones stop being visible.
SCORE_SHIFT_MIN = 4        # points, against the last recorded shift
TVL_SURGE_MIN   = 25.0     # percent, seven-day
DEADLINE_NEAR_DAYS = 7

# Scores before this date came from a model being actively rewritten:
# the permanent floor was removed, the timing curve rebased, momentum
# moved from absolute to market-relative. Movements across that window
# describe our own edits, not the market.
#
# The scoring_version check cannot catch this alone, because archive
# files written before schema 3.0 carry no version at all, so the entire
# window reads as "unchanged". This date is the backstop.
MODEL_STABLE_SINCE = "2026-11-25"

VERIFIED   = "verified"      # from a primary source we queried
DERIVED    = "derived"       # computed from data we hold
UNVERIFIED = "unverified"    # reported, not yet confirmed


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# Must match hunter_app.protocol_id exactly. Archive files written before
# schema 3.0 carry no id, so keying on "id or name" made every protocol
# appear to leave the universe and immediately rejoin it the moment the
# schema changed. Both sides get normalised through this instead.
PROTOCOL_ID_OVERRIDES = {
    "Aster Stage 6":  "aster",
    "Ink (Kraken)":   "ink",
    "Base L2":        "base",
    "Felix Protocol": "felix",
    "MegaETH":        "megaeth",
}


def protocol_key(p):
    """Stable identity for a protocol dict, whatever schema wrote it."""
    if p.get("id"):
        return p["id"]
    name = p.get("name", "")
    if name in PROTOCOL_ID_OVERRIDES:
        return PROTOCOL_ID_OVERRIDES[name]
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


# Retirements confirmed by research on 24 November. The archive cannot
# evidence these — files written before schema 3.0 have no distributed
# field — so they are seeded rather than inferred.
KNOWN_DISTRIBUTIONS = [
    ("megaeth",     "MegaETH",        "2026-04-30", "MEGA token launched via TGE"),
    ("monad",       "Monad",          "2025-11-03", "MON claim window closed; no second airdrop confirmed"),
    ("aster",       "Aster Stage 6",  "2025-09-17", "ASTER launched; Stage 6 window closed June 2026"),
    ("hyperliquid", "Hyperliquid",    "2024-11-29", "HYPE genesis drop; points programme ended, no season 2"),
    ("backpack",    "Backpack",       "2026-03-23", "BP distributed; all points seasons closed"),
]


class EventLog:
    """Append-only event store. Nothing is updated in place except the
    lifecycle field, because an observation that gets rewritten is not
    evidence."""

    def __init__(self, path=EVENT_DB):
        self.path = Path(path)
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol_id   TEXT,
                    protocol_name TEXT,
                    kind          TEXT NOT NULL,
                    headline      TEXT NOT NULL,
                    detail        TEXT,
                    occurred_at   TEXT,
                    detected_at   TEXT NOT NULL,
                    source        TEXT,
                    verification  TEXT DEFAULT 'derived',
                    lifecycle     TEXT DEFAULT 'current',
                    scan_id       TEXT,
                    payload       TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_detected ON events(detected_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_protocol ON events(protocol_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_kind ON events(kind)")

    def record(self, kind, headline, protocol_id=None, protocol_name=None,
               detail=None, occurred_at=None, source=None,
               verification=DERIVED, scan_id=None, payload=None,
               dedupe_days=None):
        """Record an event. Returns its id, or None if deduped.

        dedupe_days suppresses a repeat of the same kind for the same
        protocol within that window — without it, a deadline inside the
        alert window would fire an event on every scan, four times a day
        until it passed.
        """
        if dedupe_days and protocol_id:
            with self._conn() as c:
                row = c.execute(
                    """SELECT id FROM events
                       WHERE protocol_id=? AND kind=?
                         AND julianday('now') - julianday(detected_at) < ?
                       LIMIT 1""",
                    (protocol_id, kind, dedupe_days)).fetchone()
            if row:
                return None

        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO events
                   (protocol_id, protocol_name, kind, headline, detail,
                    occurred_at, detected_at, source, verification,
                    scan_id, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (protocol_id, protocol_name, kind, headline, detail,
                 occurred_at or _now(), _now(), source, verification,
                 scan_id, json.dumps(payload) if payload else None))
            return cur.lastrowid

    def has_event(self, protocol_id, kind):
        """Has this protocol ever recorded an event of this kind?"""
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM events WHERE protocol_id=? AND kind=? LIMIT 1",
                (protocol_id, kind)).fetchone() is not None

    # Event lifecycle.
    #
    # An event is a fact stamped at a moment, and the log is append-only,
    # so a recorded deadline cannot be rewritten when its date passes.
    # Status is therefore DERIVED at read time from the event's own date
    # against today — never stored. The log keeps saying what was true
    # when we saw it; the status says where that fact now stands.
    #
    # This is the distinction that matters for the archive: what Hunter
    # believed, and what was actually happening, have to stay separately
    # recoverable. Mutating the log to track lifecycle would collapse
    # the two and make calibration against real outcomes impossible.
    # Lifecycle and provenance are two independent axes and must not be
    # collapsed. Provenance ("verified", "derived", "unverified") says
    # how we came to know a thing: "derived" means we computed it from
    # our own data, which is sound, not doubtful. Lifecycle says where
    # the fact now stands in time. An event can perfectly well be
    # derived AND completed.
    #
    # Only events that point at a future date have a lifecycle worth
    # tracking. Everything else describes a moment that has already
    # happened, so it is complete the instant it is recorded.
    FORWARD_KINDS = {"deadline_set", "deadline_near"}

    @staticmethod
    def status_of(event, today=None):
        """Lifecycle position: announced | upcoming | completed.

        Derived from the event's own date, never stored — the log is
        append-only, so a recorded deadline cannot be rewritten when
        its date passes. The log keeps saying what was true when we
        saw it; this says where that fact now stands.
        """
        if event.get("kind") not in EventLog.FORWARD_KINDS:
            return "completed"
        when = (event.get("occurred_at") or "")[:10]
        if not when:
            return "announced"
        today = today or datetime.now(timezone.utc).date().isoformat()
        if when < today:
            return "completed"
        try:
            d = datetime.strptime(when, "%Y-%m-%d").date()
            t = datetime.strptime(today, "%Y-%m-%d").date()
        except ValueError:
            return "announced"
        return "upcoming" if (d - t).days <= 14 else "announced"

    def recent(self, days=7, kinds=None, limit=100):
        q = """SELECT * FROM events
               WHERE julianday('now') - julianday(detected_at) < ?"""
        args = [days]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        q += " ORDER BY detected_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(q, args).fetchall()]
        for r in rows:
            r["status"] = self.status_of(r)
        return rows

    def for_protocol(self, protocol_id, limit=50):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events WHERE protocol_id=? "
                "ORDER BY detected_at DESC LIMIT ?",
                (protocol_id, limit)).fetchall()]

    def stats(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT kind, COUNT(*) n FROM events GROUP BY kind "
                "ORDER BY n DESC").fetchall()
            total = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        return {"total": total, "by_kind": {r["kind"]: r["n"] for r in rows}}


def detect_events(log, previous, current, scan_id=None,
                  prev_scoring_version=None, curr_scoring_version=None):
    """Diff two scans and record what materially changed.

    `previous` and `current` are lists of protocol dicts from the
    archive. Returns the number of events recorded.

    When the scoring model itself changed between the two scans, score
    and conviction movements are NOT recorded. A protocol did not fall
    twelve points because anything happened to it — it fell because the
    timing curve was rewritten. Publishing that as an observation would
    be reporting our own internals as news, which is the failure this
    whole product has been dug out of.

    Facts about the world — distributions, deadlines, universe changes —
    are still recorded across a model change, because they are true
    regardless of how we score them.
    """
    prev = {protocol_key(p): p for p in (previous or [])}
    curr = {protocol_key(p): p for p in (current or [])}
    n = 0

    model_changed = (
        prev_scoring_version is not None
        and curr_scoring_version is not None
        and prev_scoring_version != curr_scoring_version
    )
    # Anything from before the model settled is our own editing, whatever
    # the version fields say — and before schema 3.0 they say nothing.
    if scan_id and str(scan_id)[:10] < MODEL_STABLE_SINCE:
        model_changed = True

    for pid, c in curr.items():
        name = c.get("name", pid)
        p = prev.get(pid)

        # New to the tracked universe
        if p is None:
            if log.record(ENTERED_UNIVERSE,
                          f"{name} added to the tracked universe",
                          protocol_id=pid, protocol_name=name,
                          detail=f"Entered at rank {c.get('rank')} "
                                 f"with score {c.get('score')}.",
                          source="hunter", scan_id=scan_id):
                n += 1
            continue

        # Distribution — the most consequential event there is
        if c.get("distributed") and not p.get("distributed"):
            if log.record(DISTRIBUTED,
                          f"{name} has distributed — farming window closed",
                          protocol_id=pid, protocol_name=name,
                          detail=c.get("outcome_source") or "Token detected.",
                          occurred_at=c.get("distributed_at"),
                          source=c.get("outcome_source"),
                          verification=VERIFIED, scan_id=scan_id):
                n += 1
            continue

        # A real date became known
        if c.get("deadline_iso") and not p.get("deadline_iso"):
            if log.record(DEADLINE_SET,
                          f"{name} deadline confirmed for {c['deadline_iso']}",
                          protocol_id=pid, protocol_name=name,
                          detail=f"Stated as: {c.get('deadline')}",
                          occurred_at=c.get("deadline_iso"),
                          source="protocol", verification=UNVERIFIED,
                          scan_id=scan_id):
                n += 1

        # Entering the alert window — deduped so it fires once, not 28 times
        days = c.get("days_remaining")
        if days is not None and 0 <= days <= DEADLINE_NEAR_DAYS:
            if log.record(DEADLINE_NEAR,
                          f"{name} window closes in {days} day{'s' if days != 1 else ''}",
                          protocol_id=pid, protocol_name=name,
                          detail=c.get("note", "")[:300],
                          occurred_at=c.get("deadline_iso"),
                          source="hunter", scan_id=scan_id,
                          dedupe_days=2):
                n += 1

        if c.get("deadline_passed") and not p.get("deadline_passed"):
            if log.record(DEADLINE_PASSED,
                          f"{name} window has closed",
                          protocol_id=pid, protocol_name=name,
                          detail=f"Deadline was {c.get('deadline')}.",
                          occurred_at=c.get("deadline_iso"),
                          source="hunter", scan_id=scan_id):
                n += 1

        # Conviction crossing a threshold is meaningful where a two-point
        # score move is not — it changes whether we recommend acting
        if model_changed:
            continue          # score-derived events are not observations here

        if c.get("conviction") != p.get("conviction"):
            if log.record(CONVICTION_CHANGE,
                          f"{name} moved from {p.get('conviction')} to {c.get('conviction')}",
                          protocol_id=pid, protocol_name=name,
                          detail=f"Score {p.get('score')} to {c.get('score')}.",
                          source="hunter", scan_id=scan_id, dedupe_days=1):
                n += 1

        # Material score moves only. Measured against the last recorded
        # shift rather than the previous scan, so a slow drift still
        # registers once it adds up, and jitter never does.
        ps, cs = p.get("score"), c.get("score")
        if ps is not None and cs is not None and abs(cs - ps) >= SCORE_SHIFT_MIN:
            if log.record(SCORE_SHIFT,
                          f"{name} score moved {cs - ps:+d} to {cs}",
                          protocol_id=pid, protocol_name=name,
                          detail=f"From {ps}. Confidence {c.get('confidence', 0):.2f}.",
                          source="hunter", scan_id=scan_id, dedupe_days=3):
                n += 1

        chg = c.get("tvl_change_7d")
        if chg is not None and abs(chg) >= TVL_SURGE_MIN:
            direction = "surged" if chg > 0 else "fell"
            if log.record(TVL_SURGE,
                          f"{name} TVL {direction} {abs(chg):.0f}% in seven days",
                          protocol_id=pid, protocol_name=name,
                          detail=f"Now ${(c.get('tvl') or 0)/1e6:,.0f}M.",
                          source="defillama", verification=VERIFIED,
                          scan_id=scan_id, dedupe_days=3):
                n += 1

    # Dropped out entirely. A protocol that distributed also disappears
    # from the rankings, but it completed rather than left — reporting
    # both would double-count the most important event in the log.
    for pid, p in prev.items():
        if pid in curr or p.get("distributed"):
            continue
        if log.has_event(pid, DISTRIBUTED):
            continue
        if log.record(LEFT_UNIVERSE,
                      f"{p.get('name', pid)} is no longer tracked",
                      protocol_id=pid, protocol_name=p.get("name"),
                      source="hunter", scan_id=scan_id):
            n += 1

    return n


def backfill(log, archive_dir=None, limit=None):
    """Replay the archive to populate the log with history.

    The events already happened; we simply never wrote them down. This
    recovers what the archive can still evidence — which is not
    everything, since pre-3.0 files hold only ten fields.
    """
    archive = Path(archive_dir or (APP_DIR / "briefings"))
    files = sorted(archive.glob("*.json"))
    if limit:
        files = files[-limit:]

    total, prev, pv = 0, None, None
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        curr = d.get("protocols") or []
        if not curr:
            continue
        cv = d.get("scoring_version") or (curr[0].get("scoring_version") if curr else None)
        if prev is not None:
            total += detect_events(log, prev, curr,
                                   scan_id=d.get("scan_id", f.stem),
                                   prev_scoring_version=pv,
                                   curr_scoring_version=cv)
        prev, pv = curr, cv
    return total, len(files)


def seed_known(log):
    """Record the five confirmed distributions.

    These predate schema 3.0, so the archive holds no evidence of them —
    inferring is impossible and guessing would be worse than seeding.
    """
    n = 0
    for pid, name, when, why in KNOWN_DISTRIBUTIONS:
        if log.record(DISTRIBUTED,
                      f"{name} has distributed — farming window closed",
                      protocol_id=pid, protocol_name=name, detail=why,
                      occurred_at=when, source="research 24 Nov 2026",
                      verification=VERIFIED, dedupe_days=3650):
            n += 1
    return n


if __name__ == "__main__":
    import sys
    log = EventLog()

    if "--reset" in sys.argv:
        with log._conn() as c:
            c.execute("DELETE FROM events")
        print("Event log cleared.")

    if "--backfill" in sys.argv:
        seeded = seed_known(log)
        n, scanned = backfill(log)
        print(f"Seeded {seeded} confirmed distributions")
        print(f"Replayed {scanned} archive files, recorded {n} events")
    s = log.stats()
    print(f"\nEvents: {s['total']}")
    for k, v in s["by_kind"].items():
        print(f"  {k:<20} {v}")
    print(f"\nLast 7 days:")
    for e in log.recent(days=7, limit=25):
        print(f"  {e['detected_at'][:16]}  {e['kind']:<18} {e['headline']}")
