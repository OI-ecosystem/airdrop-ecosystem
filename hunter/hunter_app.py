"""
Hunter Intelligence — Headless (Hetzner)
Version: 2.0 — Converted from v1.4 Windows/tkinter desktop app to a headless
                scan-and-exit script for systemd. Removed all tkinter/GUI
                code, the in-process 6-hour sleep loop, and file-based
                logging (hunter.log). Logging now goes to stdout only,
                captured by journald when run under systemd.
                Scoring, TVL fetch, AI briefing, archive, and Farm push
                logic is UNCHANGED from the tested/verified v1.4 — this is
                a controlled subtraction of the GUI layer, not a rewrite.
Version: 1.4 — Widened briefing archive to include full scored protocol
                list (rank, score, conviction, tvl, note, factors) per
                protocol, added schema_version + scan_id. Fixed AI briefing
                prompt to stop leaking literal "PARAGRAPH 1/2/3" labels,
                widened briefing context from top5 to top8, added a
                defensive text-cleanup pass as a safety net.
Version: 1.3 — Added persistent AI briefing archive (briefings/ directory)
Run via: hunter-scan.service (systemd oneshot), triggered by hunter-scan.timer
Log: stdout -> journald. No hunter.log file is written.
"""

import math
import json, urllib.request, urllib.error, requests
from pathlib import Path
from datetime import datetime, timezone
from datetime import timedelta

REFRESH_HOURS  = 6
DATA_FILE      = Path(__file__).parent / 'hunter_scores.json'
CONFIG_FILE    = Path(__file__).parent / 'hunter_config.json'

# ── Load config (Vercel URL etc) ──
def load_config():
    defaults = {
        "vercel_proxy_url": "https://rcloop-proxy.vercel.app",
        "vercel_proxy_route": "/api/chat",
        "ai_enabled": True
    }
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            defaults.update(saved)
        except: pass
    return defaults

CONFIG = load_config()

# ── File paths ────────────────────────────────────────
SNAPSHOT_FILE  = Path(__file__).parent / 'top5_snapshot.json'

# ── 1LINE Farm push config ────────────────────────────
FARM_ENDPOINT  = 'https://rcloop-proxy.vercel.app/api/farm-snapshot'
HUNTER_KEY     = 'Hunter333'

# ── Logging ──────────────────────────────────────────
# journald timestamps every line automatically, but the ts/level prefix is
# kept identical to the Windows version so `journalctl -u hunter-scan` reads
# the same as the old hunter.log did — no format change to get used to.
def hlog(msg, level='INFO'):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts}  [{level}]  {msg}'
    print(line, flush=True)

def hlog_sep():
    hlog('─' * 52)

# ── Protocol registry ──
PROTOCOLS = [
    {"name":"Miden","chain":"Ethereum ZK","permanent":False,"urgent":True,
     "deadline":"Snapshot closing","defillama":None,"snapshotProb":82,
     "gasUnits":120000,"chainKey":"eth",
     "factors":{"timing":22,"tvl":14,"investors":18,"team":12,"social":4,"onchain":8},
     "note":"ZK rollup. a16z + Coinbase. Snapshot closing — interact NOW. Deploy test contract, bridge assets, 3+ transactions."},
    {"name":"MegaETH","chain":"MegaETH","permanent":False,"urgent":True,
     "deadline":"Daily check-in","defillama":None,"snapshotProb":91,
     "gasUnits":60000,"chainKey":"l2",
     "factors":{"timing":20,"tvl":12,"investors":18,"team":13,"social":5,"onchain":7},
     "note":"MEGA token launched 30 Apr 2026. Farming window closed."},
    {"name":"Monad","chain":"Monad","permanent":False,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":88,
     "gasUnits":80000,"chainKey":"l2",
     "factors":{"timing":19,"tvl":11,"investors":20,"team":12,"social":5,"onchain":5},
     "note":"Mainnet live since Nov 2025. MON distributed; claim window closed 3 Nov 2025. No second airdrop confirmed."},
    {"name":"Base L2","chain":"Ethereum L2","permanent":True,"urgent":False,
     "deadline":None,"defillama":"base","snapshotProb":74,
     "gasUnits":65000,"chainKey":"l2",
     "factors":{"timing":15,"tvl":18,"investors":18,"team":8,"social":4,"onchain":4},
     "note":"Coinbase moat. 46% of all L2 TVL. Token rumoured 2026. Use Aerodrome, bridge via Base Bridge."},
    {"name":"Polymarket","chain":"Polygon","permanent":True,"urgent":False,
     "deadline":None,"defillama":"polymarket","snapshotProb":68,
     "gasUnits":55000,"chainKey":"polygon",
     "factors":{"timing":15,"tvl":14,"investors":16,"team":10,"social":5,"onchain":5},
     "note":"Token confirmed. $9B valuation. Make real predictions on active markets."},
    {"name":"Morpho","chain":"Ethereum","permanent":True,"urgent":False,
     "deadline":None,"defillama":"morpho","snapshotProb":61,
     "gasUnits":140000,"chainKey":"eth",
     "factors":{"timing":15,"tvl":16,"investors":18,"team":8,"social":3,"onchain":3},
     "note":"Paradigm-backed lending, TVL above $19B. MORPHO token already live, so no genesis drop — ongoing ecosystem incentives only. Supply USDC or WETH."},
    {"name":"Aster Stage 6","chain":"BNB Chain","permanent":False,"urgent":True,
     "deadline":"June 4 2026","defillama":None,"snapshotProb":45,
     "gasUnits":70000,"chainKey":"bnb",
     "factors":{"timing":18,"tvl":10,"investors":14,"team":9,"social":4,"onchain":7},
     "note":"ASTER launched Sep 2025; Stage 6 window closed June 2026. Phase 2 distribution complete."},
    {"name":"Hyperliquid","chain":"HyperEVM","permanent":True,"urgent":False,
     "deadline":None,"defillama":"hyperliquid","snapshotProb":55,
     "gasUnits":50000,"chainKey":"l2",
     "factors":{"timing":15,"tvl":13,"investors":14,"team":10,"social":4,"onchain":5},
     "note":"HYPE genesis drop Nov 2024. Docs confirm the points programme ended and no season 2 exists. Reserved supply is not a live farming route."},
    {"name":"MetaMask","chain":"Ethereum","permanent":True,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":52,
     "gasUnits":95000,"chainKey":"eth",
     "factors":{"timing":15,"tvl":12,"investors":16,"team":10,"social":4,"onchain":4},
     "note":"100M+ users. MASK token confirmed by Consensys, no ticker or date published. MetaMask Rewards points live — swaps, bridging, trading challenges."},
    {"name":"Aerodrome","chain":"Base","permanent":True,"urgent":False,
     "deadline":None,"defillama":"aerodrome-v2","snapshotProb":44,
     "gasUnits":72000,"chainKey":"l2",
     "factors":{"timing":15,"tvl":14,"investors":12,"team":9,"social":4,"onchain":5},
     "note":"Base dominant DEX. Lock veAERO for governance. Provide USDC/ETH liquidity."},
    {"name":"Ink (Kraken)","chain":"Ethereum L2","permanent":True,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":79,
     "gasUnits":68000,"chainKey":"l2",
     "factors":{"timing":16,"tvl":10,"investors":16,"team":9,"social":4,"onchain":3},
     "note":"Confirmed $INK airdrop. Bridge to Ink L2, interact with dApps."},
    {"name":"Variational","chain":"Arbitrum","permanent":False,"urgent":True,
     "deadline":"December 31 2026","defillama":None,"snapshotProb":83,
     "gasUnits":110000,"chainKey":"eth",
     "factors":{"timing":19,"tvl":8,"investors":16,"team":8,"social":3,"onchain":3},
     "note":"$171.8M raised (Series B Nov 2026): Dragonfly, Coinbase Ventures, Bain Capital Crypto, Peak XV. 32% genesis airdrop to points holders, fully unlocked at TGE. Trade perps on Omni (Arbitrum) — public mainnet since Nov 2026."},
    {"name":"Farcaster","chain":"Base","permanent":True,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":41,
     "gasUnits":45000,"chainKey":"l2",
     "factors":{"timing":15,"tvl":8,"investors":15,"team":9,"social":5,"onchain":4},
     "note":"Post on Warpcast, collect frames, hold native NFTs."},
    {"name":"Glider","chain":"Base","permanent":False,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":86,
     "gasUnits":60000,"chainKey":"l2",
     "factors":{"timing":20,"tvl":7,"investors":16,"team":7,"social":3,"onchain":2},
     "note":"a16z backed portfolio automation on Base. Points live."},
    {"name":"Superposition","chain":"Arbitrum L2","permanent":False,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":77,
     "gasUnits":70000,"chainKey":"l2",
     "factors":{"timing":19,"tvl":6,"investors":16,"team":7,"social":3,"onchain":1},
     "note":"a16z/Coinbase/GSR backed. Below threshold — monitor only."},
    {"name":"Loopscale","chain":"Solana","permanent":False,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":71,
     "gasUnits":30000,"chainKey":"l2",
     "factors":{"timing":18,"tvl":9,"investors":15,"team":9,"social":4,"onchain":4},
     "note":"Solana lending protocol. High TVL growth. Deposit SOL or USDC, maintain active position."},
    {"name":"TreadFi","chain":"Ethereum","permanent":False,"urgent":True,
     "deadline":"Season 1 ends soon","defillama":None,"snapshotProb":89,
     "gasUnits":85000,"chainKey":"eth",
     "factors":{"timing":21,"tvl":7,"investors":14,"team":8,"social":3,"onchain":4},
     "note":"$3.5M Founders Fund. Algorithmic trading terminal. Season 1 active — use the platform now."},
    {"name":"Felix Protocol","chain":"HyperEVM","permanent":False,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":76,
     "gasUnits":55000,"chainKey":"l2",
     "factors":{"timing":19,"tvl":8,"investors":13,"team":8,"social":3,"onchain":5},
     "note":"Liquity fork on HyperEVM. Season 2 points live. Borrow against ETH collateral."},
    {"name":"Backpack","chain":"Solana","permanent":False,"urgent":False,
     "deadline":None,"defillama":None,"snapshotProb":58,
     "gasUnits":20000,"chainKey":"l2",
     "factors":{"timing":17,"tvl":7,"investors":15,"team":9,"social":4,"onchain":3},
     "note":"BP distributed 23 Mar 2026. All points seasons and the epilogue are closed."},
    {"name":"Extended","chain":"Ethereum","permanent":False,"urgent":True,
     "deadline":"November 30 2026","defillama":None,"snapshotProb":64,
     "gasUnits":90000,"chainKey":"eth",
     "factors":{"timing":18,"tvl":6,"investors":14,"team":8,"social":3,"onchain":4},
     "note":"Perp DEX. $XTEN announced 23 Oct 2026 for November launch, 300M tokens (30%) to community. Points programme nearing its cap. Trade perps and use vaults now."},
]

# ── Scoring ──
def calc_score(p):
    s = sum(p["factors"].values())
    return max(s, 60) if p.get("permanent") else s

def conviction(s):
    return "HIGH" if s>=70 else "MODERATE" if s>=55 else "SUPPRESS"

def fmt_tvl(v):
    if not v: return None
    if v>=1e9: return f"${v/1e9:.1f}B"
    if v>=1e6: return f"${v/1e6:.0f}M"
    return f"${v/1e3:.0f}K"

# ── DefiLlama TVL fetch ──
def fetch_tvl(slug):
    try:
        url = f"https://api.llama.fi/protocol/{slug}"
        req = urllib.request.Request(url, headers={"User-Agent":"HunterIntelligence/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
        if "currentChainTvls" in d:
            return sum(d["currentChainTvls"].values())
        return d.get("tvl")
    except: return None

# ── Vercel/Haiku AI briefing ──
def fetch_ai_briefing(protocols):
    if not CONFIG.get("ai_enabled"): return None
    base_url = CONFIG.get("vercel_proxy_url","").rstrip("/")
    if not base_url: return None

    top8 = [f"#{p['rank']} {p['name']} ({p['chain']}) score:{p['score']} {p['conviction']}"
            + (f" tvl:${p['tvl']/1e9:.1f}B" if p.get('tvl') and p['tvl'] >= 1e9 else
               f" tvl:${p['tvl']/1e6:.0f}M" if p.get('tvl') else "")
            + (f" — {p['note']}" if p.get('note') else "")
            for p in protocols[:8]]
    urgent = [f"{p['name']} ({p['deadline']})" if p.get('deadline') else p['name']
              for p in protocols if p.get('urgent')]

    system = (
        "You are Hunter Intelligence, a live crypto airdrop farming intelligence system. "
        "You monitor protocols and produce actionable intelligence for people actively farming airdrops. "
        "You are not a general crypto news writer — no market commentary, no price speculation, no filler, "
        "no promotional language, no restating what Hunter is. "
        "Return exactly three plain-text paragraphs, separated by a single blank line. "
        "Never include headings, labels, numbering, bullet points, markdown, quotation marks, "
        "the words 'Paragraph 1/2/3', an introduction, or a sign-off."
    )

    prompt = (
        f"Scan date: {datetime.now().strftime('%B %d, %Y')}\n\n"
        f"Top protocols this scan:\n" + "\n".join(top8) +
        f"\n\nUrgent deadlines this scan: {', '.join(urgent) if urgent else 'None'}\n\n"
        "Write three short paragraphs, in this order, with no labels of any kind:\n"
        "1. The single most important signal in this data and why it matters to a farmer right now.\n"
        "2. The most useful specific farming actions the data supports — no invented numbers or promises.\n"
        "3. A protocol further down the list that is being underrated relative to its score or movement.\n"
        "Keep each paragraph tight, roughly 40-60 words — but prioritise genuine, specific insight over "
        "hitting an exact word count. Sharp and specific, never padded."
    )

    payload = json.dumps({"system": system, "prompt": prompt}).encode()

    try:
        url = base_url + "/api/ai"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if data.get("content") and data["content"][0]:
            text = data["content"][0].get("text","").strip()
            return _clean_briefing_text(text)
    except Exception as e:
        print(f"AI briefing error: {e}")
    return None


def _clean_briefing_text(text):
    """Defensive net — strips leaked PARAGRAPH labels even if the prompt fix isn't 100% reliable.
    Never touches the substantive wording, only removes label-only lines/prefixes."""
    import re
    label_line   = re.compile(r"^\s*paragraph\s*\d+\s*:?\s*$", re.IGNORECASE)
    inline_label = re.compile(r"^\s*paragraph\s*\d+\s*:\s*", re.IGNORECASE)
    lines = [l for l in text.split("\n") if not label_line.match(l.strip())]
    lines = [inline_label.sub("", l) for l in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())

# ── Main scan ──
def push_to_farm(snapshot_data):
    """Push top5 snapshot to Vercel for 1LINE Farm. Fully logged. Never blocks app."""
    hlog(f"[Farm] Pushing to Vercel: {FARM_ENDPOINT}")
    hlog(f"[Farm] Protocols in snapshot: {len(snapshot_data.get('protocols',[]))}")
    hlog(f"[Farm] Market regime: {snapshot_data.get('market_regime','?')}")
    try:
        response = requests.post(
            FARM_ENDPOINT,
            json=snapshot_data,
            headers={"Content-Type": "application/json", "X-Hunter-Key": HUNTER_KEY},
            timeout=15
        )
        if response.status_code == 200:
            result = response.json()
            hlog(f"[Farm] SUCCESS — stored at {result.get('stored_at','?')}")
        else:
            hlog(f"[Farm] Push returned {response.status_code}: {response.text[:200]}", "WARN")
    except requests.exceptions.Timeout:
        hlog("[Farm] Push timed out — will retry next scan", "WARN")
    except requests.exceptions.RequestException as e:
        hlog(f"[Farm] Push failed: {type(e).__name__}: {e}", "ERROR")
    except Exception as e:
        hlog(f"[Farm] Unexpected error: {type(e).__name__}: {e}", "ERROR")


# ── Full-scan publication + deadline hygiene ──────────────────────────
SCORES_ENDPOINT = "https://rcloop-proxy.vercel.app/api/scores"

DEADLINE_FORMATS = ("%B %d %Y", "%d %B %Y", "%Y-%m-%d", "%b %d %Y", "%d %b %Y")


def parse_deadline(text):
    """Return a date if the deadline string is a real date, else None."""
    if not text:
        return None
    cleaned = str(text).replace(",", "").strip()
    for fmt in DEADLINE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def apply_deadlines(results):
    """Expire passed deadlines; attach machine-readable date fields.

    Only values that parse as real dates are ever expired. Descriptive
    values such as "Snapshot closing" or "Daily check-in" are left
    untouched, so this can never suppress a live opportunity.
    """
    today = datetime.now(timezone.utc).date()
    expired = 0
    for r in results:
        d = parse_deadline(r.get("deadline"))
        if d is None:
            r["deadline_iso"] = None
            r["deadline_passed"] = False
            r["days_remaining"] = None
            continue
        delta = (d - today).days
        r["deadline_iso"] = d.isoformat()
        r["days_remaining"] = delta
        r["deadline_passed"] = delta < 0
        if delta < 0 and r.get("urgent"):
            r["urgent"] = False
            expired += 1
            hlog(f"  Deadline passed — urgent cleared: {r['name']} "
                 f"({r['deadline']}, {abs(delta)} days ago)", "WARN")
    return expired


def push_scores(scores_data):
    """Publish the full scan so the public site can render live data.

    Mirrors push_to_farm(): same auth, same failure handling, never
    blocks the scan.
    """
    hlog(f"[Scores] Pushing to Vercel: {SCORES_ENDPOINT}")
    hlog(f"[Scores] Protocols in payload: {len(scores_data.get('protocols', []))}")
    try:
        response = requests.post(
            SCORES_ENDPOINT,
            json=scores_data,
            headers={"Content-Type": "application/json", "X-Hunter-Key": HUNTER_KEY},
            timeout=15
        )
        if response.status_code == 200:
            result = response.json()
            hlog(f"[Scores] SUCCESS — stored at {result.get('stored_at','?')} "
                 f"({result.get('count','?')} protocols, {result.get('kept_in','?')})")
        else:
            hlog(f"[Scores] Push returned {response.status_code}: {response.text[:200]}", "WARN")
    except requests.exceptions.Timeout:
        hlog("[Scores] Push timed out — will retry next scan", "WARN")
    except requests.exceptions.RequestException as e:
        hlog(f"[Scores] Push failed: {type(e).__name__}: {e}", "ERROR")
    except Exception as e:
        hlog(f"[Scores] Unexpected error: {type(e).__name__}: {e}", "ERROR")


# ── Delayed briefing for the public site ──────────────────────────────
# The archive is already on disk; this just surfaces the briefing from
# roughly a week ago so the public page can show a call alongside what
# actually happened since. The current briefing stays for subscribers.

DELAYED_BRIEFING_DAYS = 7


def get_delayed_briefing(days=DELAYED_BRIEFING_DAYS):
    """Return (briefing_text, scan_id) from ~`days` ago, or (None, None).

    Picks the archived scan closest to the target date. Never raises:
    a missing or unreadable archive simply means no delayed briefing
    this cycle.
    """
    try:
        archive = Path(__file__).parent / "briefings"
        if not archive.is_dir():
            return None, None

        target = datetime.now(timezone.utc).date() - timedelta(days=days)
        best, best_gap = None, None

        for f in archive.rglob("*.json"):
            stem = f.stem                      # e.g. 2026-11-17-0600
            try:
                d = datetime.strptime(stem[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            gap = abs((d - target).days)
            if best_gap is None or gap < best_gap:
                best, best_gap = f, gap

        # Only use it if we landed within two days of the target
        if best is None or best_gap > 2:
            return None, None

        data = json.loads(best.read_text())
        text = data.get("ai_briefing")
        if not text:
            return None, None
        return text, data.get("scan_id", best.stem)

    except Exception as e:
        hlog(f"[Delayed] Could not read archive: {type(e).__name__}: {e}", "WARN")
        return None, None


# ── Identity, retirement and integration slots ────────────────────────

# Stable IDs. Derived from the name, with overrides where the display
# name is likely to change ("Aster Stage 6" becomes Stage 7 eventually).
# Without this, a rename reads as one protocol vanishing and another
# appearing, and the archive silently loses its history.
PROTOCOL_ID_OVERRIDES = {
    "Aster Stage 6":  "aster",
    "Ink (Kraken)":   "ink",
    "Base L2":        "base",
    "Felix Protocol": "felix",
    "MegaETH":        "megaeth",
}


def protocol_id(name):
    if name in PROTOCOL_ID_OVERRIDES:
        return PROTOCOL_ID_OVERRIDES[name]
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


# Protocols confirmed distributed by research on 24 Nov 2026. Automatic
# retirement below only sees protocols we hold a DefiLlama slug for, so
# these are recorded explicitly. Each stays in the archive — a completed
# call is evidence for the track record, not something to delete.
CONFIRMED_DISTRIBUTED = {
    "megaeth":     ("2026-04-30", "MEGA token launched via TGE"),
    "monad":       ("2025-11-03", "MON claim window closed; no second airdrop confirmed"),
    "aster":       ("2025-09-17", "ASTER token launched; Stage 6 window closed June 2026"),
    "hyperliquid": ("2024-11-29", "HYPE genesis drop; docs confirm points programme ended, no season 2"),
    "backpack":    ("2026-03-23", "BP distributed; all points seasons closed"),
}


def apply_identity_and_retirement(results):
    """Attach IDs, retire distributed protocols, reserve integration slots.

    Retirement is evidence-led: an explicit confirmation, or a market cap
    appearing on a protocol we previously saw as tokenless. A retired
    protocol keeps its score for the record but leaves the rankings — it
    is no longer an opportunity, whatever its merits were.
    """
    retired = 0
    for r in results:
        pid = protocol_id(r.get("name", ""))
        r["id"] = pid

        # Reserved — Sentinel writes outcomes here, Signal writes context
        r.setdefault("distributed", False)
        r.setdefault("distributed_at", None)
        r.setdefault("outcome_source", None)
        r.setdefault("context", None)

        if pid in CONFIRMED_DISTRIBUTED:
            when, why = CONFIRMED_DISTRIBUTED[pid]
            r["distributed"] = True
            r["distributed_at"] = when
            r["outcome_source"] = why
            r["conviction"] = "DISTRIBUTED"
            r["urgent"] = False
            retired += 1
            hlog(f"  Retired (distributed {when}): {r['name']} — {why}")

    return retired


# ── DefiLlama feed: TVL, retirement, discovery ────────────────────────
try:
    from hunter_feed import DefiLlamaFeed, DiscoveryQueue
    FEED_AVAILABLE = True
except Exception as _e:                       # pragma: no cover
    FEED_AVAILABLE = False
    _FEED_IMPORT_ERROR = str(_e)


def run_feed(results):
    """Resolve TVL, retire distributed protocols, observe candidates.

    Returns (tvl_count, retired_count). Never raises: a DefiLlama outage
    must not cost us a scan, so every failure degrades to the previous
    behaviour and says so in the log.
    """
    if not FEED_AVAILABLE:
        hlog(f"[Feed] hunter_feed unavailable ({_FEED_IMPORT_ERROR}) — "
             "falling back to per-slug fetch", "WARN")
        return 0, 0, None

    try:
        feed = DefiLlamaFeed()
        feed.load()
    except Exception as e:
        hlog(f"[Feed] Could not load DefiLlama: {type(e).__name__}: {e}", "ERROR")
        return 0, 0, None

    tvl_count = retired = 0

    for r in results:
        if r.get("distributed"):
            continue                          # already retired

        try:
            tvl, source = feed.resolve_tvl(r)
        except Exception as e:
            hlog(f"[Feed] TVL error for {r.get('name')}: {type(e).__name__}", "WARN")
            tvl, source = None, None

        if tvl:
            r["tvl"] = tvl
            r["tvl_source"] = source
            tvl_count += 1
            shown = f"${tvl/1e9:.2f}B" if tvl >= 1e9 else f"${tvl/1e6:.0f}M"
            hlog(f"  TVL {r['name']}: {shown} ({source})")

        try:
            distributed, symbol = feed.check_distributed(r)
        except Exception:
            distributed, symbol = False, None

        if distributed:
            r["distributed"] = True
            r["distributed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            r["outcome_source"] = f"DefiLlama reports token {symbol}"
            r["conviction"] = "DISTRIBUTED"
            r["urgent"] = False
            retired += 1
            hlog(f"  AUTO-RETIRED: {r['name']} — token {symbol} detected, "
                 "farming window closed", "WARN")

    # Discovery: observe and age candidates. Admission comes later.
    try:
        candidates = feed.find_candidates()
        if candidates:
            queue = DiscoveryQueue()
            new, seen = queue.observe(candidates)
            ready = queue.ready_to_admit({c["slug"] for c in candidates})
            if ready:
                names = ", ".join(f"{r['name']} (${r['tvl_latest']/1e6:.0f}M)"
                                  for r in ready)
                hlog(f"  {len(ready)} candidate(s) cleared quarantine: {names}")
            stats = queue.stats()
            hlog(f"  Candidate queue: {stats}")
    except Exception as e:
        hlog(f"[Feed] Discovery error: {type(e).__name__}: {e}", "WARN")

    return tvl_count, retired, feed


# ── Live scoring ──────────────────────────────────────────────────────
# SCORING_VERSION is written into every record. Scores produced before
# this change came from constants and are not comparable with these.

SCORING_VERSION = 4

TVL_FACTOR_MAX       = 20
ONCHAIN_FACTOR_MAX   = 15
ONCHAIN_NEUTRAL      = 7.5    # a flat week is neither good nor bad
ONCHAIN_SCALE        = 20.0   # a 20% week reaches ~76% of the spread
TIMING_FACTOR_MAX    = 25
TIMING_HALFLIFE_DAYS   = 21   # urgency halves every three weeks
TIMING_CONFIRMED_BASE  = 12   # worth this much simply to know the date
TIMING_SPREAD          = 13   # proximity adds up to this on top


def tvl_points(tvl):
    """0-20 from TVL on a log scale, or None without live data.

    $1M scores 0, $100M 8, $1bn 12, $10bn 16, $100bn 20. Kept to one
    decimal so ordinary movement registers instead of being rounded away.
    """
    if not tvl or tvl <= 0:
        return None
    pts = 4.0 * math.log10(tvl / 1e6)
    return round(max(0.0, min(float(TVL_FACTOR_MAX), pts)), 1)


def timing_points(days_remaining):
    """12-25 from deadline proximity, or None without a real date.

    Knowing the date is itself worth 12 points; proximity adds up to 13
    on top. An earlier version decayed to a floor of 3, which meant a
    protocol with a confirmed date two months out scored below one with
    no date at all — better information producing a worse score.

    A passed deadline still returns 0. That is not missing information,
    it is the window having closed.
    """
    if days_remaining is None:
        return None
    if days_remaining < 0:
        return 0.0
    decay = TIMING_SPREAD * (0.5 ** (days_remaining / TIMING_HALFLIFE_DAYS))
    return round(TIMING_CONFIRMED_BASE + decay, 1)


def onchain_points(change_7d, market_median=None):
    """0-15 from seven-day TVL momentum relative to the market.

    Scored on the gap to the market median rather than the raw number.
    In a week where everything is down 8%, a protocol down 4% is
    outperforming by four points and a protocol down 11% is lagging by
    three — the absolute figures would mark both down together.

    Falls back to absolute scoring if no market median is available,
    which is worse but better than nothing.

    tanh keeps one dramatic week from dominating: four points of
    outperformance scores 9.9, twenty points scores 13.2. Direction and
    persistence matter more than a single spike, and a spike is often
    one large depositor.
    """
    if change_7d is None:
        return None
    relative = change_7d - market_median if market_median is not None else change_7d
    spread = ONCHAIN_FACTOR_MAX - ONCHAIN_NEUTRAL
    pts = ONCHAIN_NEUTRAL + spread * math.tanh(relative / ONCHAIN_SCALE)
    return round(max(0.0, min(float(ONCHAIN_FACTOR_MAX), pts)), 1)


def score_all(results, feed=None):
    """Score every protocol, substituting live factors where data allows.

    Must run AFTER TVL and deadlines are resolved. Returns the count of
    live factors applied, which is the honest measure of how much of the
    scoring is actually responding to the world.
    """
    live = 0
    market = feed.market_momentum() if feed else None
    if market is not None:
        hlog(f"  Market 7d median {market:+.2f}% — momentum scored relative to it")

    for r in results:
        authored = dict(r.get("factors") or {})
        factors = dict(authored)
        sources = {k: "static" for k in factors}
        live_here = 0

        tp = tvl_points(r.get("tvl"))
        if tp is not None:
            factors["tvl"] = tp
            sources["tvl"] = "live"
            live += 1
            live_here += 1

        chg = feed.momentum(r.get("id")) if feed else None
        if chg is not None:
            op = onchain_points(chg, market)
            if op is not None:
                factors["onchain"] = op
                sources["onchain"] = "live"
                r["tvl_change_7d"] = chg
                if market is not None:
                    r["tvl_change_7d_relative"] = round(chg - market, 2)
                live += 1
                live_here += 1

        gp = timing_points(r.get("days_remaining"))
        if gp is not None:
            authored_timing = authored.get("timing", 0)
            if gp == 0.0:
                # Window closed — a hard override, not a comparison
                factors["timing"] = 0.0
                sources["timing"] = "live-expired"
                live += 1
                live_here += 1
            elif gp > authored_timing:
                factors["timing"] = gp
                sources["timing"] = "live"
                live += 1
                live_here += 1
            else:
                # Confirmed date, but the authored judgement rates it
                # higher. Keep the authored value: knowing the date
                # should never cost a protocol points.
                sources["timing"] = "static-floor"

        # The permanent floor (max(s, 60)) was removed in scoring v3. It
        # existed to protect established protocols from thin data, and
        # it was flattening the middle of the table instead: three
        # protocols sat at exactly 60 because their real scores were
        # below it. With live TVL and momentum the data is no longer
        # thin, so a low score is now information rather than an artefact.
        s = sum(factors.values())

        # How much of this score is measurement and how much is the
        # judgement someone typed in August. Published, because a
        # subscriber deserves to know which calls rest on evidence.
        total_factors = len(factors) or 1
        r["confidence"]       = round(live_here / total_factors, 2)
        r["factors_live"]     = live_here
        r["factors_total"]    = total_factors

        r["score"]            = round(s)
        r["conviction"]       = conviction(r["score"])
        r["factors"]          = {k: round(v, 1) for k, v in factors.items()}
        r["factors_authored"] = authored
        r["factor_sources"]   = sources
        r["scoring_version"]  = SCORING_VERSION

    return live



# ── Payload extras: events, delayed newsletter, sparkline history ─────

def recent_events(days=7, limit=40):
    """Events for the site. Never fatal: the scan matters more."""
    try:
        from hunter_events import EventLog
        rows = EventLog().recent(days=days, limit=limit)
        return [{k: r.get(k) for k in
                 ("kind", "headline", "detail", "protocol_id", "protocol_name",
                  "occurred_at", "detected_at", "verification", "status")}
                for r in rows]
    except Exception as e:
        hlog(f"[Payload] events unavailable: {type(e).__name__}: {e}", "WARN")
        return []


def delayed_newsletter(days=7):
    """The issue from a week ago, if one was built."""
    try:
        d = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        folder = Path(__file__).parent / "newsletters"
        if not folder.is_dir():
            return None
        best, gap = None, None
        for f in folder.glob("hunter-weekly-*.json"):
            try:
                when = datetime.strptime(f.stem.replace("hunter-weekly-", ""),
                                         "%Y-%m-%d").date()
            except ValueError:
                continue
            g = abs((when - d).days)
            if gap is None or g < gap:
                best, gap = f, g
        if best is None or gap > 4:
            return None
        payload = json.loads(best.read_text())
        return {"issue_date": payload.get("issue_date"),
                "markdown": payload.get("markdown"),
                "days_delayed": days}
    except Exception as e:
        hlog(f"[Payload] newsletter unavailable: {type(e).__name__}: {e}", "WARN")
        return None


def score_history(results, points=30):
    """Recent score per protocol, for sparklines.

    One point per archived scan, oldest first. Reads only schema 3.0
    files, which are the ones carrying a stable id — matching on display
    name across the schema boundary is what produced a phantom mass
    exodus in the event log, and the same trap applies here.
    """
    try:
        archive = Path(__file__).parent / "briefings"
        if not archive.is_dir():
            return {}
        files = sorted(archive.glob("*.json"))[-points:]
        series = {}
        for f in files:
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if d.get("schema_version") != "3.0":
                continue
            for p in d.get("protocols", []):
                pid = p.get("id")
                if not pid:
                    continue
                series.setdefault(pid, []).append(p.get("score"))
        return {k: v for k, v in series.items() if len(v) >= 2}
    except Exception as e:
        hlog(f"[Payload] history unavailable: {type(e).__name__}: {e}", "WARN")
        return {}


def run_scan():
    hlog_sep()
    hlog("SCAN STARTED")
    hlog(f"  Script:    {Path(__file__).name}")
    hlog(f"  Data file: {DATA_FILE}")
    hlog(f"  Snapshot:  {SNAPSHOT_FILE}")

    # Build the working set. Scoring is deliberately deferred until TVL
    # and deadlines are resolved below — scoring here would use inputs
    # from before the feed ran, which is how a "live" score ends up
    # being a constant.
    results = [dict(p, score=None, conviction=None, tvl=None) for p in PROTOCOLS]
    retired = apply_identity_and_retirement(results)
    if retired:
        hlog(f"  {retired} protocol(s) retired as distributed — excluded from rankings")
    results = [r for r in results if not r.get("distributed")]

    tvl_count, auto_retired, _scan_feed = run_feed(results)
    if auto_retired:
        hlog(f"  {auto_retired} protocol(s) auto-retired this scan "
             "(token detected on DefiLlama)")
        retired += auto_retired
        results = [r for r in results if not r.get("distributed")]
    hlog(f"  Live TVL resolved for {tvl_count} protocol(s)")

    expired = apply_deadlines(results)
    if expired:
        hlog(f"  {expired} stale deadline(s) cleared this scan")

    live_factors = score_all(results, feed=_scan_feed)
    hlog(f"  Scored {len(results)} protocols — {live_factors} live factor(s) applied "
         f"(scoring v{SCORING_VERSION})")

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results): r["rank"] = i + 1

    above  = sum(1 for r in results if r["conviction"] != "SUPPRESS")
    urgent = sum(1 for r in results if r.get("urgent"))
    hlog(f"  Scored: {len(results)} protocols — {above} above threshold — {urgent} urgent")
    hlog(f"  Top 3: {results[0]['name']} ({results[0]['score']}), {results[1]['name']} ({results[1]['score']}), {results[2]['name']} ({results[2]['score']})")

    # AI briefing
    hlog("Fetching AI briefing from Vercel...")
    ai_briefing = fetch_ai_briefing(results)
    if ai_briefing:
        hlog(f"AI briefing received ({len(ai_briefing)} chars)")
    else:
        hlog("AI briefing unavailable — continuing without it", "WARN")

    # Write hunter_scores.json
    _delayed_text, _delayed_scan = get_delayed_briefing()
    if _delayed_text:
        hlog(f"  Delayed briefing sourced from scan {_delayed_scan}")
    else:
        hlog("  No delayed briefing available yet (archive too short)")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    output = {
        "generated_at": ts,
        "ai_briefing":  ai_briefing,
        "ai_briefing_delayed":      _delayed_text,
        "ai_briefing_delayed_scan": _delayed_scan,
        "ai_briefing_delayed_days": DELAYED_BRIEFING_DAYS,
        "summary": {
            "total":            len(results),
            "above_threshold":  above,
            "urgent":           urgent,
            "live_tvl_count":   sum(1 for r in results if r.get("tvl")),
        },
        "protocols": results,
    }
    output["events"] = recent_events()
    output["newsletter_delayed"] = delayed_newsletter()
    output["history"] = score_history(results)
    hlog(f"  Payload: {len(output['events'])} event(s), "
         f"history for {len(output['history'])} protocol(s), "
         f"newsletter {'attached' if output['newsletter_delayed'] else 'none yet'}")

    DATA_FILE.write_text(json.dumps(output, indent=2))
    hlog(f"hunter_scores.json written — {above}/{len(results)} above threshold")
    push_scores(output)

    # Write top5_snapshot.json
    top5 = results[:5]
    top3_avg      = sum(r["score"] for r in results[:3]) / 3
    market_regime = "Aggressive" if top3_avg >= 70 else "Defensive"
    snapshot = {
        "snapshot_version": "1.0",
        "generated_at":     ts,
        "generated_by":     "Hunter Intelligence — Sentinel v2.0",
        "valid_for_hours":  REFRESH_HOURS,
        "market_regime":    market_regime,
        "next_scan_eta":    f"{REFRESH_HOURS}h 00m",
        "summary": {
            "total_protocols_scored": len(results),
            "above_threshold":        above,
            "urgent_count":           urgent,
        },
        "protocols": [
            {
                "rank":         p["rank"],
                "name":         p["name"],
                "chain":        p["chain"],
                "score":        p["score"],
                "conviction":   p["conviction"],
                "urgent":       p.get("urgent", False),
                "deadline":     p.get("deadline"),
                "snapshotProb": p.get("snapshotProb", 50),
                "gasUnits":     p.get("gasUnits", 80000),
                "chainKey":     p.get("chainKey", "eth"),
                "tags":         p.get("tags", []),
                "note":         p.get("note", ""),
            }
            for p in top5
        ],
        "filter_tags": {
            "low_gas":         [p["name"] for p in top5 if p.get("chainKey") in ["l2","polygon","bnb"]],
            "ethereum":        [p["name"] for p in top5 if "Ethereum" in p.get("chain","")],
            "l2":              [p["name"] for p in top5 if p.get("chainKey") == "l2"],
            "urgent":          [p["name"] for p in top5 if p.get("urgent")],
            "high_conviction": [p["name"] for p in top5 if p.get("conviction") == "HIGH"],
            "snapshot_open":   [p["name"] for p in top5 if p.get("snapshotProb", 0) >= 60],
            "low_risk":        [p["name"] for p in top5 if p.get("snapshotProb", 0) >= 70 and p["score"] >= 60],
        }
    }
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))
    hlog(f"top5_snapshot.json written — top 5: {[p['name'] for p in top5]}")
    hlog(f"Market regime: {market_regime}")

    # ── Archive AI briefing (permanent history) ──
    try:
        briefing_dir = Path(__file__).parent / 'briefings'
        briefing_dir.mkdir(exist_ok=True)
        briefing_file = briefing_dir / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}.json"
        if not briefing_file.exists():
            briefing_file.write_text(json.dumps({
                "schema_version":  "3.0",
                "scan_id":         datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M"),
                "generated_at":    ts,
                "scoring_version": SCORING_VERSION,
                "top5":            [p["name"] for p in results[:5]],
                "above_threshold": above,
                "urgent":          urgent,
                "ai_briefing":     ai_briefing or "",
                # Schema 2.0 archived an explicit ten-field whitelist, so
                # every field added later was computed and then thrown
                # away at this boundary. The archive is the asset — it
                # keeps the whole record now, and new fields arrive
                # without anyone having to remember this line exists.
                # Only browser-rendering fields are dropped: presentation,
                # not evidence.
                "protocols": [
                    {k: v for k, v in p.items()
                     if k not in ("gasUnits", "chainKey", "snapshotProb")}
                    for p in results
                ],
            }, indent=2))
            hlog(f"Briefing archived: {briefing_file.name}")
        else:
            hlog(f"Briefing already exists for this timestamp — skipping")
    except Exception as e:
        hlog(f"Briefing archive failed: {e}", "WARN")

    # Push to Vercel for 1LINE Farm
    push_to_farm(snapshot)

    hlog_sep()
    hlog("SCAN COMPLETE")
    hlog(f"  generated_at: {ts}")
    hlog(f"  Next scan in: {REFRESH_HOURS}h")
    hlog_sep()

    return output


# ══════════════════════════════════════════════════
#  HEADLESS ENTRYPOINT
#  No GUI, no background loop, no threading. One scan, then exit.
#  Recurrence is provided entirely by hunter-scan.timer (systemd).
# ══════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        run_scan()
    except Exception as e:
        hlog(f"FATAL — scan did not complete: {type(e).__name__}: {e}", "ERROR")
        raise
