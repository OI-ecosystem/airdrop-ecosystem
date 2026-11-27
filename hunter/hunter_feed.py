#!/usr/bin/env python3
"""Hunter Intelligence — DefiLlama feed, retirement and discovery.

One fetch of /protocols does three jobs:

    tvl                       live TVL for tracked protocols
    symbol present            the protocol has a token, so it has distributed
    recent + no symbol        a discovery candidate

Chains (Monad, Ink, MegaETH) are not protocols on DefiLlama and need the
chains endpoint, which is why slugs for them silently returned nothing.

Matching is by explicit slug only, verified against the expected chain.
Name matching is deliberately not attempted: DefiLlama's "Superposition"
is an Aptos lending protocol, nothing to do with the Arbitrum L2 of the
same name, and a silent mismatch like that is invisible once it reaches
a chart.

Discovery quarantines candidates rather than admitting them. A protocol
must keep qualifying for a week before it can enter the rankings — time
doing the work a human approval queue would otherwise do.

Usage:
    from hunter_feed import DefiLlamaFeed, DiscoveryQueue

    feed = DefiLlamaFeed()
    tvl, source = feed.resolve_tvl(protocol)
    distributed, symbol = feed.check_distributed(protocol)
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
QUEUE_DB = APP_DIR / "hunter_candidates.db"

PROTOCOLS_URL = "https://api.llama.fi/protocols"
CHAINS_URL = "https://api.llama.fi/v2/chains"
CACHE_TTL = 3 * 3600

# Protocols tracked as DefiLlama *chains* rather than protocols.
# Keyed by Hunter's protocol id.
CHAIN_MAP = {
    "monad":   "Monad",
    "megaeth": "MegaETH",
    "ink":     "Ink",
    "base":    "Base",
}

# Explicit protocol slugs, with the chain we expect DefiLlama to report.
# The chain check is the guard against name collisions.
# Candidate slugs per protocol, tried in order against /protocol/{slug}.
# The first entry should be the PARENT where one exists: DefiLlama's
# /protocols array lists children only, so a list lookup for "morpho"
# misses and a name match lands on "Morpho Blue" — $9.3bn instead of the
# $19.1bn parent aggregate. Right-looking, wrong number.
SLUG_MAP = {
    "polymarket":  (["polymarket", "polymarket-international"], "polygon"),
    "morpho":      (["morpho", "morpho-blue"], "ethereum"),
    "aerodrome":   (["aerodrome", "aerodrome-v2", "aerodrome-slipstream"], "base"),
    "loopscale":   (["loopscale", "loopscale-lending"], "solana"),
    "felix":       (["felix", "felix-cdp", "felix-protocol"], "hyperliquid"),
    "extended":    (["extended-perps", "extended"], "ethereum"),
}

# Perp DEXes hold collateral rather than locked capital, so DefiLlama
# reports little or no TVL for them even when they are busy. Variational
# and Tread.fi both read $0 while trading actively. Volume is the right
# measure for these and lives on a different endpoint; until that is
# wired, their tvl factor stays as authored rather than being scored at
# zero, which would be a worse answer than admitting we cannot see it.
VOLUME_BASED = {"variational", "treadfi", "extended"}

# Discovery thresholds. Deliberately stricter than the Sentinel defaults,
# because nothing here is reviewed by a human before it reaches paying
# subscribers — the filter has to be the safety net.
MIN_TVL = 5_000_000
MIN_AGE_DAYS = 30
QUARANTINE_DAYS = 7
MAX_ADMIT_PER_RUN = 2
MAX_TRACKED = 20

# Categories a person can actually farm — somewhere to deposit, trade or
# interact, with a plausible future allocation to users.
#
# Without this, "tokenless with large TVL" describes every centralised
# exchange on DefiLlama. Bybit has $13bn of custodial balances and no
# token, and is never going to airdrop to depositors. Nor are Coinbase
# Bridge, Binance staked ETH, or risk curators like Gauntlet, which are
# service providers rather than protocols with a user base to reward.
FARMABLE_CATEGORIES = {
    "dexs", "dex aggregator", "prop amm",
    "lending", "cdp", "leveraged farming", "rwa lending",
    "derivatives", "options", "synthetics", "basis trading",
    "yield", "yield aggregator", "farm", "liquidity manager",
    "restaking", "liquid restaking",
    "launchpad", "prediction market", "nft marketplace",
    "privacy", "payments", "indexes", "telegram bot", "ai agents",
    "gaming", "insurance", "rwa", "options vault",
}

# Names that signal a custodial venue however DefiLlama categorises them
CUSTODIAL_MARKERS = (
    "binance", "bybit", "okx", "kucoin", "huobi", "htx", "bitfinex",
    "bitget", "gemini", "kraken", "coinbase", "crypto-com", "deribit",
    "bitstamp", "poloniex", "hashkey", "osl", "upbit", "bithumb",
    "mexc", "gate.io", "backpack exchange", "robinhood",
)


def _log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  [{level}]  [Feed] {msg}", flush=True)


class DefiLlamaFeed:
    """One cached fetch of the protocol and chain lists per run."""

    def __init__(self):
        self._protocols = None
        self._chains = None
        self._fetched_at = 0
        self._momentum = {}        # protocol id -> 7-day TVL change, percent
        self._market_median = None
        self.errors = []

    def market_momentum(self):
        """Median seven-day TVL change across protocols above $10M.

        The reference point for relative scoring. In a week where the
        whole market is down 8%, a protocol down 4% is outperforming and
        one down 11% is lagging — absolute momentum hides both, and marks
        everything down equally, which discriminates between nothing.
        """
        self.load()
        if self._market_median is not None:
            return self._market_median
        if not self._protocols:
            return None

        vals = []
        for p in self._protocols.values():
            tvl = p.get("tvl") or 0
            chg = p.get("change_7d")
            if tvl >= 10_000_000 and isinstance(chg, (int, float)):
                vals.append(float(chg))

        if len(vals) < 50:
            _log(f"Only {len(vals)} protocols with 7d change — "
                 "not enough for a market median", "WARN")
            return None

        vals.sort()
        mid = len(vals) // 2
        self._market_median = round(
            vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)
        _log(f"Market 7d median: {self._market_median:+.2f}% "
             f"across {len(vals)} protocols above $10M")
        return self._market_median

    def momentum(self, pid):
        """Seven-day TVL change as a percentage, or None.

        Populated as a side effect of resolve_tvl, so call that first.
        Used to score the onchain factor: a protocol whose TVL grew this
        week demonstrably has more activity than one that shrank, which
        is what the factor is meant to capture and what a hand-authored
        value from August cannot know.
        """
        return self._momentum.get(pid)

    @staticmethod
    def _change_from_history(series, days=7):
        """Percentage change over `days` from a DefiLlama TVL series."""
        if not isinstance(series, list) or len(series) < days + 1:
            return None
        try:
            now = series[-1].get("totalLiquidityUSD")
            then = series[-(days + 1)].get("totalLiquidityUSD")
            if not now or not then or then <= 0:
                return None
            return round((now - then) / then * 100.0, 2)
        except (AttributeError, TypeError, IndexError):
            return None

    # ── fetching ───────────────────────────────────────────────────────

    def _get(self, url, retries=3):
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "HunterIntelligence/2.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    _log(f"Rate limited, waiting {wait}s", "WARN")
                    time.sleep(wait)
                    continue
                self.errors.append(f"HTTP {e.code} on {url}")
                _log(f"HTTP {e.code} fetching {url}", "ERROR")
                return None
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                self.errors.append(f"{type(e).__name__} on {url}")
                _log(f"{type(e).__name__} fetching {url}: {e}", "ERROR")
                return None
        return None

    def load(self):
        """Fetch both lists. Safe to call repeatedly — cached."""
        if self._protocols is not None and time.time() - self._fetched_at < CACHE_TTL:
            return

        protos = self._get(PROTOCOLS_URL)
        chains = self._get(CHAINS_URL)

        self._protocols = {}
        if isinstance(protos, list):
            for p in protos:
                slug = p.get("slug")
                if slug:
                    self._protocols[slug] = p
            _log(f"Loaded {len(self._protocols)} protocols")
        else:
            _log("Protocol list unavailable this run", "WARN")

        self._chains = {}
        if isinstance(chains, list):
            for c in chains:
                name = c.get("name")
                if name:
                    self._chains[name.lower()] = c
            _log(f"Loaded {len(self._chains)} chains")
        else:
            _log("Chain list unavailable this run", "WARN")

        self._fetched_at = time.time()

    # ── resolution ─────────────────────────────────────────────────────

    def resolve_tvl(self, protocol):
        """Return (tvl, source) for a tracked protocol.

        source is 'protocol', 'chain' or None. Failures are logged rather
        than swallowed — a silently absent TVL is how two of the five
        configured slugs went unnoticed for three months.
        """
        self.load()
        pid = protocol.get("id") or ""

        if pid in CHAIN_MAP:
            key = CHAIN_MAP[pid].lower()
            entry = self._chains.get(key)
            if entry and entry.get("tvl"):
                return float(entry["tvl"]), "chain"
            _log(f"No chain TVL for {protocol.get('name')} (looked for '{key}')", "WARN")
            return None, None

        if pid in SLUG_MAP:
            slugs, expected_chain = SLUG_MAP[pid]
            name = protocol.get("name", pid)

            # 1. The direct endpoint first. Parent protocols are served
            #    here but absent from the /protocols array, so this is the
            #    only route to an aggregate figure.
            for slug in slugs:
                data = self._get(f"https://api.llama.fi/protocol/{slug}", retries=2)
                if not data:
                    continue
                chains = [c.lower() for c in (data.get("chains") or [])]
                if chains and not any(expected_chain in c for c in chains):
                    _log(f"REJECTED '{slug}' for {name}: DefiLlama reports {chains}, "
                         f"expected {expected_chain}. Possible name collision.", "ERROR")
                    continue
                chg = self._change_from_history(data.get("tvl"))
                if chg is not None:
                    self._momentum[pid] = chg

                tvl = data.get("currentChainTvls")
                if isinstance(tvl, dict) and tvl:
                    total = sum(v for v in tvl.values() if isinstance(v, (int, float)) and v > 0)
                    if total:
                        if slug != slugs[0]:
                            _log(f"{name}: resolved via '{slug}'")
                        return float(total), "protocol"
                if data.get("tvl") and isinstance(data["tvl"], (int, float)):
                    return float(data["tvl"]), "protocol"

            # 2. The list, in case a child entry is all that exists.
            entry = self._match_by_name(name, expected_chain)
            if entry and entry.get("tvl"):
                if entry.get("change_7d") is not None:
                    self._momentum[pid] = round(float(entry["change_7d"]), 2)
                _log(f"{name}: no parent entry, using child "
                     f"'{entry.get('name')}' ({entry.get('slug')}) — "
                     "may understate the full protocol", "WARN")
                return float(entry["tvl"]), "protocol-child"

            _log(f"No DefiLlama entry for {name} (tried {slugs} and name match)", "WARN")
            return None, None

        return None, None

    def _match_by_name(self, name, expected_chain):
        """Last resort: a child entry whose name begins with ours.

        Matches on Hunter's own protocol name rather than the slug, since
        a slug like "aerodrome-v2" is too specific to match "Aerodrome
        Slipstream". The chain check still applies, so the Superposition
        trap — an Aptos lender sharing a name with an Arbitrum L2 — stays
        closed. Returns the largest match where a protocol has several
        components, and the caller flags the result as possibly partial.
        """
        want = name.lower().replace(" protocol", "").strip()
        matches = []
        for p in self._protocols.values():
            pname = (p.get("name") or "").lower()
            if pname == want or pname.startswith(want + " "):
                chains = [c.lower() for c in (p.get("chains") or [])]
                if chains and not any(expected_chain in c for c in chains):
                    continue
                if p.get("tvl"):
                    matches.append(p)
        if not matches:
            return None
        return max(matches, key=lambda x: x.get("tvl") or 0)

    def check_distributed(self, protocol):
        """Has this protocol acquired a token since we started tracking it?

        A symbol on DefiLlama means a token exists, which means any
        genesis airdrop has happened. Returns (distributed, symbol).
        """
        self.load()
        pid = protocol.get("id") or ""

        if pid in SLUG_MAP:
            slug, _ = SLUG_MAP[pid]
            entry = self._protocols.get(slug)
            if entry:
                sym = entry.get("symbol")
                if sym and sym != "-":
                    return True, sym

        if pid in CHAIN_MAP:
            entry = self._chains.get(CHAIN_MAP[pid].lower())
            if entry:
                sym = entry.get("tokenSymbol")
                if sym:
                    return True, sym

        return False, None

    # ── discovery ──────────────────────────────────────────────────────

    def find_candidates(self):
        """Tokenless protocols with real size and a track record.

        Stricter than Sentinel's defaults on purpose: $5M rather than
        $500k, and at least 30 days listed, because nothing downstream
        reviews these before they reach subscribers.
        """
        self.load()
        if not self._protocols:
            return []

        cutoff = time.time() - (MIN_AGE_DAYS * 86400)
        out = []

        rejected = {"token": 0, "tvl": 0, "age": 0, "category": 0, "custodial": 0}

        for slug, p in self._protocols.items():
            sym = p.get("symbol")
            if sym and sym != "-":
                rejected["token"] += 1
                continue                      # already has a token

            tvl = p.get("tvl") or 0
            if tvl < MIN_TVL:
                rejected["tvl"] += 1
                continue

            listed = p.get("listedAt") or 0
            if not listed or listed > cutoff:
                rejected["age"] += 1
                continue                      # too new to judge

            category = (p.get("category") or "").lower()
            if category not in FARMABLE_CATEGORIES:
                rejected["category"] += 1
                continue                      # exchange, bridge, curator, wrapper

            name_l = (p.get("name") or "").lower()
            if any(m in name_l for m in CUSTODIAL_MARKERS):
                rejected["custodial"] += 1
                continue                      # custodial venue in disguise

            out.append({
                "slug":        slug,
                "name":        p.get("name", ""),
                "chain":       p.get("chain", ""),
                "chains":      p.get("chains") or [],
                "category":    p.get("category", ""),
                "tvl":         float(tvl),
                "change_1d":   p.get("change_1d"),
                "change_7d":   p.get("change_7d"),
                "url":         p.get("url", ""),
                "twitter":     p.get("twitter", ""),
                "listed_at":   datetime.fromtimestamp(listed, tz=timezone.utc).strftime("%Y-%m-%d"),
                "description": (p.get("description") or "")[:400],
            })

        out.sort(key=lambda x: -x["tvl"])
        _log(f"{len(out)} farmable candidates "
             f"(rejected: {rejected['token']} have tokens, {rejected['tvl']} too small, "
             f"{rejected['age']} too new, {rejected['category']} wrong category, "
             f"{rejected['custodial']} custodial)")
        return out


class DiscoveryQueue:
    """Quarantine for discovered protocols.

    A candidate must keep qualifying across QUARANTINE_DAYS of scans
    before it can be admitted. Time does the work a human approval queue
    would otherwise do, which is what makes hands-off operation safe:
    anything that spikes and vanishes never survives the wait.
    """

    def __init__(self, db_path=QUEUE_DB):
        self.path = Path(db_path)
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS candidates (
                    slug          TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    chain         TEXT,
                    category      TEXT,
                    first_seen    TEXT NOT NULL,
                    last_seen     TEXT NOT NULL,
                    times_seen    INTEGER DEFAULT 1,
                    tvl_first     REAL,
                    tvl_latest    REAL,
                    status        TEXT DEFAULT 'quarantine',
                    admitted_at   TEXT,
                    rejected_why  TEXT,
                    payload       TEXT
                )
            """)

    def observe(self, candidates):
        """Record this run's sightings. Returns (new, updated)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new = updated = 0
        with self._conn() as c:
            for cand in candidates:
                row = c.execute("SELECT slug, times_seen FROM candidates WHERE slug=?",
                                (cand["slug"],)).fetchone()
                if row:
                    c.execute("""UPDATE candidates
                                 SET last_seen=?, times_seen=times_seen+1,
                                     tvl_latest=?, payload=?
                                 WHERE slug=?""",
                              (today, cand["tvl"], json.dumps(cand), cand["slug"]))
                    updated += 1
                else:
                    c.execute("""INSERT INTO candidates
                                 (slug,name,chain,category,first_seen,last_seen,
                                  tvl_first,tvl_latest,payload)
                                 VALUES (?,?,?,?,?,?,?,?,?)""",
                              (cand["slug"], cand["name"], cand["chain"],
                               cand["category"], today, today,
                               cand["tvl"], cand["tvl"], json.dumps(cand)))
                    new += 1
        if new or updated:
            _log(f"Candidates: {new} new, {updated} still qualifying")
        return new, updated

    def ready_to_admit(self, seen_today):
        """Candidates that survived quarantine and still qualify today."""
        cutoff = (datetime.now(timezone.utc).timestamp() - QUARANTINE_DAYS * 86400)
        out = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM candidates WHERE status='quarantine' ORDER BY tvl_latest DESC"
            ).fetchall()
        for r in rows:
            if r["slug"] not in seen_today:
                continue                       # stopped qualifying
            first = datetime.strptime(r["first_seen"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if first.timestamp() > cutoff:
                continue                       # still serving its week
            out.append(dict(r))
        return out[:MAX_ADMIT_PER_RUN]

    def mark_admitted(self, slug):
        with self._conn() as c:
            c.execute("UPDATE candidates SET status='admitted', admitted_at=? WHERE slug=?",
                      (datetime.now(timezone.utc).strftime("%Y-%m-%d"), slug))

    def stats(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) n FROM candidates GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}


if __name__ == "__main__":
    feed = DefiLlamaFeed()
    cands = feed.find_candidates()
    print(f"\n{len(cands)} candidates passing the filter\n")
    print(f"{'NAME':<28} {'CHAIN':<14} {'TVL':>12}  {'LISTED':<12} CATEGORY")
    print("-" * 90)
    for c in cands[:25]:
        print(f"{c['name'][:27]:<28} {c['chain'][:13]:<14} "
              f"${c['tvl']/1e6:>10,.1f}M  {c['listed_at']:<12} {c['category'][:22]}")
    if feed.errors:
        print(f"\nErrors: {feed.errors}")
