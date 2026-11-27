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
# Incremented whenever PROTOCOLS changes in a way that moves scores:
# factors edited, entries retired, classes reassigned. Stamped into every
# archive file so the event log can tell "we changed our records" apart
# from "the market moved" — a date-based cutoff cannot, because editing
# the list and the market moving happen on the same day.
#
# 1  original hand-authored list
# 2  27 Nov 2026 audit: 9 retired, classes and entry windows added,
#    factors re-derived from sourced research
PROTOCOL_LIST_VERSION = 2

PROTOCOLS = [
    # ── OPEN: a new entrant can still qualify ──────────────────────────
    {"name":"Variational","chain":"Arbitrum","permanent":False,"urgent":False,
     "cls":"pre_token_open","chainKey":"l2","gasUnits":110000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"open",
     "eligibility":"Trade perps on Omni (Arbitrum). Weekly points accruing, no snapshot announced.",
     "eligibility_tier":"official",
     "defillama":None,
     "factors":{"timing":17,"tvl":10,"investors":20,"team":11,"social":4,"onchain":9},
     "note":"Confirmed 32% of supply to points holders, fully unlocked at launch. "
            "$171.8M raised: $110M Series B 18 Nov 2026 at $1.5B led by Bain Capital Crypto "
            "with Dragonfly, HSG, Peak XV, 50T Funds; $50M Series A led by Dragonfly May 2026. "
            "Also Coinbase Ventures, Hack VC. Founders Lucas V. Schuermann, Edward Yu. "
            "VAR expected Q4 2026, no date disclosed. Omni charges zero trading fees, "
            "so cost to farm is Arbitrum gas only."},

    # ── ROLLING: eligibility accrues, no announced close ───────────────
    {"name":"Miden","chain":"Ethereum ZK","permanent":False,"urgent":False,
     "cls":"pre_token_rolling","chainKey":"eth","gasUnits":120000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"rolling",
     "eligibility":"10% of supply planned primarily for holders and stakers of Polygon POL. "
                   "Secondary route via testnet activity. Snapshots began May 2025, ongoing.",
     "eligibility_tier":"official",
     "defillama":None,
     "factors":{"timing":12,"tvl":0,"investors":18,"team":12,"social":3,"onchain":5},
     "note":"$25M seed 29 Apr 2025 led by a16z crypto, 1kx and Hack VC, with Finality "
            "Capital, Symbolic Capital, P2 Ventures, Delta Fund, MH Ventures. Angels "
            "include Rune Christensen (MakerDAO) and Sreeram Kannan (EigenLayer). "
            "Founders Bobbin Threadbare, Dominik Schmid, Azeem Khan, former Meta engineers, "
            "spun out of Polygon Labs. Mainnet slipped from Q4 2025 to 2026, reward date TBA. "
            "NOTE: previously carried a deadline of 'Snapshot closing' and urgent=True. "
            "No such deadline exists and the main route in is holding POL, not farming."},

    {"name":"Polymarket","chain":"Polygon","permanent":False,"urgent":False,
     "cls":"pre_token_rolling","chainKey":"polygon","gasUnits":90000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"rolling",
     "eligibility":"No criteria published. Polymarket's help pages state no airdrop or TGE "
                   "has been announced. Trading activity is the presumed route but is not confirmed.",
     "eligibility_tier":"reported",
     "defillama":"polymarket",
     "factors":{"timing":10,"tvl":16,"investors":14,"team":10,"social":4,"onchain":8},
     "note":"Promised, not scheduled. CMO Matthew Modabber confirmed a token and airdrop in "
            "Oct 2025, tied to the US relaunch, which happened May 2026 — but the official "
            "help pages still say nothing has been announced. Reported 5-10% of supply, not "
            "official. Collateral moved to pUSD in an Apr 2026 upgrade. "
            "WARNING: circulating 'eligibility criteria' for POLY (volume gates, allocation "
            "percentages, eligibility checkers) are fabrications on third-party sites. No "
            "official channel asks for a signature to check eligibility."},

    # ── CLOSED: eligibility already determined, operator not in it ─────
    {"name":"Extended","chain":"Arc","permanent":False,"urgent":False,
     "cls":"pre_token_closed","chainKey":"l2","gasUnits":100000,
     "deadline":"Signing closes 08:30 UTC 29 Nov","deadline_iso":"2026-11-29",
     "entry_window":"closed","entry_closed_on":"2026-11-06",
     "eligibility":"Extended Points programme. Final snapshot completed 6 Nov 2026 — "
                   "nothing done now creates eligibility. If already eligible: sign the "
                   "Community Allocation Terms in-app before 08:30 UTC 29 Nov for the first batch.",
     "eligibility_tier":"official",
     "defillama":None,
     "factors":{"timing":0,"tvl":8,"investors":10,"team":9,"social":3,"onchain":6},
     "note":"XTEN: 300,000,000 tokens, 30% of 1B supply. Contract on Arc "
            "0x10DEDcBc9601aeC2A941EDbB4D13FcA7Fc820f38. First batch 29 Nov; spot trading "
            "15:00 UTC 30 Nov; signing window closes 23:59 UTC 31 Dec after which unsigned "
            "allocations move to the Ecosystem Fund. Founded by former Revolut staff. "
            "Staked XTEN and XTEN in the 7-day unstaking period cannot be transferred. "
            "Scored to zero on timing because the window is shut: unreachable, not weak."},

    # ── TOKEN LIVE: not airdrop farming. Kept for context, not ranked ──
    {"name":"Morpho","chain":"Ethereum","permanent":False,"urgent":False,
     "cls":"token_live","chainKey":"eth","gasUnits":140000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"closed",
     "eligibility":"Token already distributed. Ongoing MORPHO rewards require providing "
                   "liquidity in Morpho vaults, scaled by deposit size and duration. "
                   "That is capital-at-risk yield farming, not airdrop farming.",
     "eligibility_tier":"official",
     "defillama":"morpho",
     "factors":{"timing":0,"tvl":20,"investors":16,"team":12,"social":4,"onchain":12},
     "note":"MORPHO transferable since 21 Nov 2024. Max supply 1B, market cap around $1.29B, "
            "listed on Binance, Kraken, Bybit, MEXC, Gate, KuCoin, Bitget. 4.9% already "
            "distributed to protocol users and launch pool participants. "
            "Was ranked 2nd at 70 in an airdrop tracker for a token trading two years."},

    {"name":"Aerodrome","chain":"Base","permanent":False,"urgent":False,
     "cls":"token_live","chainKey":"l2","gasUnits":95000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"closed",
     "eligibility":"AERO live since 2023. Any rewards are liquidity incentives, not an airdrop.",
     "eligibility_tier":"reported",
     "defillama":"aerodrome-v2",
     "factors":{"timing":0,"tvl":14,"investors":10,"team":9,"social":3,"onchain":10},
     "note":"AERO has been trading since 2023. Reported as renamed to Aero. "
            "UNVERIFIED: needs the same write-up as Morpho before it is shown anywhere."},

    # ── UNVERIFIED: no airdrop confirmed either way. Not ranked. ───────
    {"name":"Base L2","chain":"Base","permanent":False,"urgent":False,
     "cls":"unverified","chainKey":"l2","gasUnits":80000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"unknown",
     "eligibility":"No token and no airdrop announced. Jesse Pollak reported on 21 Nov 2026 "
                   "as saying Base is 'continuing to explore, but also not being in a rush'.",
     "eligibility_tier":"reported",
     "defillama":"base",
     "factors":{"timing":6,"tvl":18,"investors":14,"team":11,"social":4,"onchain":10},
     "note":"Coinbase's L2. Large and real, but no airdrop has been announced and the "
            "most recent signal is explicitly non-committal. Previously carried a "
            "permanent score floor of 60, which the browser kept applying for two days "
            "after it was removed from the engine."},

    {"name":"MetaMask","chain":"Ethereum","permanent":False,"urgent":False,
     "cls":"unverified","chainKey":"eth","gasUnits":100000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"unknown",
     "eligibility":"Long-rumoured, never announced. No criteria from any official source.",
     "eligibility_tier":"rumour",
     "defillama":None,
     "factors":{"timing":5,"tvl":0,"investors":12,"team":10,"social":4,"onchain":6},
     "note":"UNVERIFIED. Rumoured for years with nothing official. Kept on the watchlist "
            "only because of its size. Must not be ranked or recommended on rumour."},

    {"name":"Ink (Kraken)","chain":"Ink","permanent":False,"urgent":False,
     "cls":"unverified","chainKey":"l2","gasUnits":85000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"unknown",
     "eligibility":"Not verified. Kraken's L2. Research outstanding.",
     "eligibility_tier":"rumour",
     "defillama":None,
     "factors":{"timing":6,"tvl":12,"investors":11,"team":10,"social":3,"onchain":8},
     "note":"UNVERIFIED. TVL resolves via name match. Note that Nado, built on Ink by the "
            "Kraken team, closed a points programme on 12 Nov 2026 with 33.2M points across "
            "45,000+ users and no token announced — that is the more interesting target and "
            "it is not tracked."},

    {"name":"Felix Protocol","chain":"Hyperliquid","permanent":False,"urgent":False,
     "cls":"unverified","chainKey":"l2","gasUnits":90000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"unknown",
     "eligibility":"Not verified. Research outstanding.",
     "eligibility_tier":"rumour",
     "defillama":None,
     "factors":{"timing":6,"tvl":8,"investors":9,"team":8,"social":2,"onchain":7},
     "note":"UNVERIFIED. CDP protocol on Hyperliquid. No airdrop confirmed either way."},

    {"name":"Loopscale","chain":"Solana","permanent":False,"urgent":False,
     "cls":"unverified","chainKey":"l2","gasUnits":5000,
     "deadline":None,"deadline_iso":None,
     "entry_window":"unknown",
     "eligibility":"Not verified. Research outstanding.",
     "eligibility_tier":"rumour",
     "defillama":None,
     "factors":{"timing":6,"tvl":10,"investors":10,"team":8,"social":2,"onchain":7},
     "note":"UNVERIFIED. Solana lending. No airdrop confirmed either way."},
]

# Retired on 27 Nov 2026 after an X-source audit found no token and, in one
# case, no pulse. Kept here as a record so they are not silently re-added.
#
#   Superposition  Official account silent since 21 May 2025. Users publicly
#                  reporting stuck Aptos lending positions they cannot repay
#                  or withdraw (15 and 27 Nov 2026). Listing a protocol whose
#                  users cannot withdraw is a liability, not a stale record.
#   Glider         Official statement 19 Nov 2026: "We've never announced a
#                  token." Points frozen 11 Nov, Discord roles snapshotted
#                  19 Nov, historical points converted to fee discounts.
#   TreadFi        No token, points or snapshot announcement. Official posts
#                  are product-only; the foundation account is silent.
#   Farcaster      Official account posted two podcast links in a fortnight.
#                  No token, snapshot or eligibility language anywhere.
#
# Previously retired via CONFIRMED_DISTRIBUTED: MegaETH, Monad, Aster Stage 6,
# Hyperliquid, Backpack.
RETIRED_NO_TOKEN = {
    "Superposition": "Dormant since May 2025; users reporting stuck funds",
    "Glider":        "Protocol confirmed it has never announced a token",
    "TreadFi":       "No token, points or snapshot signal",
    "Farcaster":     "No token, snapshot or eligibility signal",
}

# ── Which protocols belong in the ranking ───────────────────────────────
#
# The operator holds no positions in any tracked protocol, so a closed
# window is worth zero to him however strong the protocol is, and a live
# token is a different product entirely. Only classes that he can still
# act on are ranked. The rest are kept and shown, in their own sections,
# because "we checked this and you cannot get in" is information too.

RANKABLE_CLASSES = ("pre_token_open", "pre_token_rolling")

CLASS_LABELS = {
    "pre_token_open":    "Open",
    "pre_token_rolling": "Rolling",
    "pre_token_closed":  "Closed",
    "token_live":        "Token live",
    "unverified":        "Unverified",
}


def is_rankable(p):
    """True if a new entrant could still qualify for this protocol."""
    return p.get("cls", "unverified") in RANKABLE_CLASSES


def source_tier(p):
    """official | reported | rumour — governs what may be stated as fact.

    Eligibility criteria are the most dangerous field Hunter carries. A
    search for Polymarket's criteria returns, top of the page, a site
    with fabricated telemetry and an invented volume gate for an airdrop
    the protocol says it has never announced. Anything not from the
    protocol itself is a discovery hint, never a published criterion.
    """
    return p.get("eligibility_tier", "rumour")


ELIGIBILITY_WARNING = (
    "No legitimate airdrop requires signing a message or granting wallet "
    "access to check eligibility. Criteria shown here come only from the "
    "protocol's own site or verified account; anything else is marked as "
    "reported or unverified and should not be acted on."
)


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
        # An explicitly authored deadline_iso wins over parsing the prose.
        #
        # This function used to derive the date solely from the display
        # string, so an entry carrying a precise, sourced date alongside
        # readable prose lost the date entirely: Extended's
        # "Signing closes 08:30 UTC 29 Nov" does not parse, and its
        # deadline_iso of 2026-11-29 was being overwritten with None —
        # silently removing the one deadline on the board that mattered.
        authored_iso = r.get("deadline_iso")
        d = None
        if authored_iso:
            try:
                d = datetime.strptime(str(authored_iso)[:10], "%Y-%m-%d").date()
            except ValueError:
                hlog(f"  Unparseable deadline_iso on {r['name']}: "
                     f"{authored_iso!r} — falling back to the display string", "WARN")
        if d is None:
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

    # Rank only what the operator can still act on.
    #
    # Sorting the whole list by score put a token that has been trading
    # for two years second, and an unverified protocol third, above the
    # only confirmed open window. Score measures how promising a protocol
    # is; it says nothing about whether you can get in. Both matter, and
    # conflating them is what made the table misleading rather than
    # merely thin.
    #
    # Everything else keeps its score and is still published, in its own
    # section: "we checked, and you cannot get in" is information too.
    ranked   = [r for r in results if is_rankable(r)]
    unranked = [r for r in results if not is_rankable(r)]

    ranked.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(ranked):
        r["rank"] = i + 1

    unranked.sort(key=lambda x: x["score"], reverse=True)
    for r in unranked:
        r["rank"] = None          # explicitly not ranked, not rank 0
        r["rank_excluded_because"] = CLASS_LABELS.get(
            r.get("cls", "unverified"), r.get("cls"))

    results[:] = ranked + unranked
    hlog(f"    Ranked {len(ranked)} actionable; {len(unranked)} held back "
         f"(closed, already issued, or unverified)")

    above  = sum(1 for r in results if r["conviction"] != "SUPPRESS")
    urgent = sum(1 for r in results if r.get("urgent"))
    hlog(f"  Scored: {len(results)} protocols — {above} above threshold — {urgent} urgent")
    hlog(f"  Top 3: {results[0]['name']} ({results[0]['score']}), {results[1]['name']} ({results[1]['score']}), {results[2]['name']} ({results[2]['score']})")

    # ── Event detection ────────────────────────────────────────────────
    #
    # This was missing entirely. detect_events was only ever reachable
    # through `hunter_events.py --backfill`, run by hand, so in normal
    # six-hourly operation Hunter recorded no events at all: the log
    # froze between manual rebuilds, and the newsletter and the site's
    # "what changed" section were both reading a static table. An event
    # log that only updates when someone remembers to update it is a
    # worse lie than no event log.
    #
    # Never fatal: a scan that scores correctly but fails to log an event
    # is still a good scan.
    try:
        from hunter_events import EventLog, detect_events
        scan_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
        prev_scan = None
        _arch = Path(__file__).parent / "briefings"
        if _arch.is_dir():
            for _f in sorted(_arch.glob("*.json"), reverse=True):
                try:
                    _d = json.loads(_f.read_text())
                except Exception:
                    continue
                # Compare only against schema 3.0: pre-3.0 files lack the
                # stable id, and keying on display name is what produced
                # 35 phantom retirement events in the first backfill.
                if _d.get("schema_version") == "3.0" and _d.get("protocols"):
                    prev_scan = _d
                    break
        if prev_scan:
            n_events = detect_events(
                EventLog(),
                prev_scan.get("protocols") or [],
                results,
                scan_id=scan_id,
                prev_scoring_version=prev_scan.get("scoring_version"),
                curr_scoring_version=SCORING_VERSION,
                prev_list_version=prev_scan.get("protocol_list_version"),
                curr_list_version=PROTOCOL_LIST_VERSION)
            hlog(f"  Events recorded: {n_events} "
                 f"(vs {prev_scan.get('scan_id', 'previous scan')})")
        else:
            hlog("  No schema 3.0 scan to compare against — no events this run")
    except Exception as e:
        hlog(f"  Event detection failed: {type(e).__name__}: {e}", "WARN")

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
    # The top five of what is actionable, not of everything scored.
    # results[:5] swept in a two-year-old traded token and an unverified
    # protocol, which is exactly what the class split exists to prevent.
    # If fewer than five are actionable, publish fewer — a short honest
    # list beats a padded one.
    top5 = [r for r in results if is_rankable(r)][:5]
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
            # These two used to filter on snapshotProb, a number that was
            # never computed from anything. They now filter on facts: can
            # a new entrant still qualify, and did the protocol itself say
            # so rather than an aggregator.
            "entry_open":      [p["name"] for p in top5 if p.get("entry_window") == "open"],
            "officially_confirmed": [p["name"] for p in top5
                                     if p.get("eligibility_tier") == "official"],
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
                "protocol_list_version": PROTOCOL_LIST_VERSION,
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
