#!/usr/bin/env python3
"""Check the server is actually able to run a scan, before it runs one.

Every failure in the 27 Nov 2026 deployment was an environment problem
that no amount of sandbox testing could have caught: a database owned by
root so the scan could not write events, a credential read from an
environment variable that was never set, and an endpoint that answered
401. Each surfaced separately, mid-run, as a different-looking error.

This collapses that class of failure into one loud report before any
work starts. It writes nothing and pushes nothing — it is safe to run
whenever, and it is meant to be run as the service user:

    sudo -u hunter python3 preflight.py

Exit status is 0 if a scan can run, 1 if something would fail.
"""
import json
import os
import pwd
import sys
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent

GREEN, RED, AMBER, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = AMBER = OFF = ""

problems, warnings = [], []


def ok(label, detail=""):
    print(f"  {GREEN}ok{OFF}    {label}" + (f" — {detail}" if detail else ""))


def bad(label, detail, fix):
    print(f"  {RED}FAIL{OFF}  {label} — {detail}")
    problems.append((label, detail, fix))


def warn(label, detail, fix=""):
    print(f"  {AMBER}warn{OFF}  {label} — {detail}")
    warnings.append((label, detail, fix))


def section(name):
    print(f"\n{name}")


# ── Who are we ──────────────────────────────────────────────────────────
section("Identity")
me = pwd.getpwuid(os.geteuid()).pw_name
if me == "root":
    warn("running as root",
         "the timers run as 'hunter', so this will not detect permission "
         "problems that will affect them",
         "re-run: sudo -u hunter python3 preflight.py")
else:
    ok("running as", me)


# ── Writable paths ──────────────────────────────────────────────────────
#
# This is the check that matters most. A file created by a root-run
# command stays root-owned, and the next scan as 'hunter' fails on it.
# It happened to hunter_events.db and hunter_trajectory.json, and the
# scan reported it as "attempt to write a readonly database" — which
# reads like corruption, not ownership.
section("Write access")

MUST_WRITE_FILES = ["hunter_scores.json", "top5_snapshot.json",
                    "hunter_events.db", "hunter_trajectory.json"]
MUST_WRITE_DIRS = ["briefings", "newsletters"]

for name in MUST_WRITE_FILES:
    f = APP / name
    if not f.exists():
        ok(name, "absent — will be created")
        continue
    try:
        owner = pwd.getpwuid(f.stat().st_uid).pw_name
    except KeyError:
        owner = str(f.stat().st_uid)
    if os.access(f, os.W_OK):
        ok(name, f"writable (owner {owner})")
    else:
        bad(name, f"NOT writable, owned by {owner}",
            f"sudo chown {me}:{me} {f}")

for name in MUST_WRITE_DIRS:
    d = APP / name
    if not d.is_dir():
        warn(name + "/", "directory missing", f"mkdir -p {d}")
        continue
    if os.access(d, os.W_OK | os.X_OK):
        n = len(list(d.glob("*.json")))
        ok(name + "/", f"writable, {n} file(s)")
    else:
        bad(name + "/", "NOT writable",
            f"sudo chown -R {me}:{me} {d}")

# Anything else in the directory the service user cannot write
stray = []
for f in APP.iterdir():
    if f.is_file() and f.suffix in (".json", ".db") and not os.access(f, os.W_OK):
        stray.append(f.name)
if stray:
    bad("other unwritable data files", ", ".join(sorted(stray)),
        f"sudo chown {me}:{me} " + " ".join(f"{APP}/{s}" for s in sorted(stray)))


# ── Config and credentials ──────────────────────────────────────────────
section("Configuration")
cfg_path = APP / "hunter_config.json"
cfg = {}
if not cfg_path.exists():
    bad("hunter_config.json", "missing",
        f"create {cfg_path} containing at least "
        '{"hunter_key": "...", "vercel_proxy_url": "https://..."}')
else:
    try:
        cfg = json.loads(cfg_path.read_text())
        ok("hunter_config.json", f"{len(cfg)} key(s)")
    except Exception as e:
        bad("hunter_config.json", f"unreadable: {type(e).__name__}: {e}",
            "fix the JSON syntax")

key = cfg.get("hunter_key") or os.environ.get("HUNTER_KEY", "")
if not key:
    bad("hunter_key", "not set in config or environment",
        'add {"hunter_key": "<the rotated key>"} to hunter_config.json')
elif key == "Hunter333":
    # This value was committed to a public repository on 27 Nov 2026.
    # Anyone can read it, and it authorises writes to the scores and
    # farm endpoints — which means publishing arbitrary protocol
    # recommendations to subscribers.
    bad("hunter_key", "is the PUBLICLY EXPOSED value 'Hunter333'",
        "rotate it: set a new value in the Vercel env AND in "
        "hunter_config.json. The old key must be treated as burned.")
else:
    ok("hunter_key", f"set ({len(key)} chars)")

if not cfg.get("vercel_proxy_url"):
    warn("vercel_proxy_url", "not set, falling back to the built-in default")
else:
    ok("vercel_proxy_url", cfg["vercel_proxy_url"])


# ── Modules import ──────────────────────────────────────────────────────
#
# A module that fails at import takes the whole scan with it. One of
# today's fixes called hlog() a few lines above its own definition,
# which is a NameError at import and a completely dead engine — worse
# than any wrong number.
section("Modules")
sys.path.insert(0, str(APP))
for mod in ("hunter_feed", "hunter_events", "hunter_app", "build_newsletter"):
    if not (APP / f"{mod}.py").exists():
        warn(mod, "not present")
        continue
    try:
        __import__(mod)
        ok(mod, "imports")
    except Exception as e:
        bad(mod, f"import failed: {type(e).__name__}: {e}",
            "the scan cannot run at all until this is fixed")


# ── Endpoints ───────────────────────────────────────────────────────────
#
# Read-only probes. A GET tells us the proxy is up; it cannot confirm a
# write would be authorised, so a 401 on push is still possible — but a
# missing route or a dead proxy shows up here instead of mid-scan.
section("Endpoints")
base = (cfg.get("vercel_proxy_url") or "https://rcloop-proxy.vercel.app").rstrip("/")
for route, label in (("/api/scores", "scores"), ("/api/ai", "ai")):
    url = base + route
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hunter-preflight"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(400).decode("utf-8", "replace")
        ok(label, f"HTTP {r.status}")
        if route == "/api/scores":
            try:
                stored = json.loads(body).get("stored_at")
                if stored:
                    ok("scores freshness", f"last stored {stored}")
            except Exception:
                pass
    except urllib.error.HTTPError as e:
        # 405 on a GET to a POST-only route is healthy, not broken.
        if e.code in (405, 400):
            ok(label, f"HTTP {e.code} to GET — route exists, expects POST")
        elif e.code == 401:
            warn(label, "HTTP 401 — route exists but rejected us; "
                        "check the key matches the Vercel env")
        else:
            bad(label, f"HTTP {e.code}", "check the Vercel deployment")
    except Exception as e:
        bad(label, f"unreachable: {type(e).__name__}: {e}",
            "check network and the proxy URL")


# ── Verdict ─────────────────────────────────────────────────────────────
print()
if problems:
    print(f"{RED}{len(problems)} problem(s) would stop or degrade a scan:{OFF}\n")
    for label, detail, fix in problems:
        print(f"  {label}: {detail}")
        if fix:
            print(f"    fix: {fix}")
    print()
    sys.exit(1)

if warnings:
    print(f"{AMBER}{len(warnings)} warning(s), scan can run.{OFF}")
    for label, detail, fix in warnings:
        if fix:
            print(f"  {label}: {fix}")
    print()
    sys.exit(0)

print(f"{GREEN}All checks passed — a scan can run.{OFF}\n")
sys.exit(0)
