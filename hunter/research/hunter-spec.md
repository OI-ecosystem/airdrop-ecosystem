# Hunter Intelligence — working specification

**Last verified: 27 November 2026.** This file is the source of truth for
what Hunter tracks and why. It exists because the tracked list previously
lived only in a conversation context, which decays: after one compaction
the list was rebuilt from memory and four protocols were invented while
four real ones were dropped. Read this before any work on Hunter. Correct
it when it is wrong rather than working around it.

**Rule: the server is the source of truth for state. This file is the
source of truth for intent.** Never assert the current protocol list,
score, or deadline from memory — read it off the server.

---

## 1. What Hunter is for

Hunter identifies pre-token DeFi protocols where an airdrop is plausible
and an entry window is still open, and tells the operator what to do and
by when. Its output must be actionable: chain, route in, dated deadline
with a time, and cost.

Everything else — rankings, scores, trajectories — is supporting context
for that. The event is the product.

## 2. The operator's position (decides all ranking)

**Garry holds no positions or points in any tracked protocol** (confirmed
27 Nov 2026). This is the single most important ranking input and the old
model had no concept of it.

Consequences:

- A protocol whose snapshot has already closed is worth **zero** to him,
  however strong it is. Extended scored 66 and ranked 3rd while being
  unreachable.
- A protocol requiring a token he does not hold (Miden needs POL) is a
  capital decision, not a farming decision, and must be labelled as such.
- An open window with a confirmed allocation outranks everything. There
  is currently one of those.

If he later takes positions, this section changes and rankings change
with it. A `positions.json` is the intended mechanism.

## 3. The three classes

The old model scored all of these on one 0–100 scale, which is why a
two-year-old $1.29B traded token ranked second in an airdrop tracker.
They are different products and must not share a ranking.

| Class | Meaning | Belongs in main ranking |
|---|---|---|
| `pre_token_open` | No token, entry window open and actionable | **Yes — this is the product** |
| `pre_token_rolling` | No token, eligibility accrues with no announced close | Yes, flagged as undated |
| `pre_token_closed` | Eligibility already determined, operator not in it | No — archive only |
| `token_live` | Token issued and trading; any rewards are capital-at-risk yield | No — separate view or retire |
| `retire` | No token ever announced, abandoned, or operationally broken | No — remove |

## 4. Tracked protocols — verified status

Fifteen live on the server as of the 00:01 UTC scan on 27 Nov 2026.

### Open and actionable

**Variational** — `pre_token_open` — Arbitrum
- Confirmed 32% of supply to points holders, fully unlocked at launch
- Entry route: weekly Omni points, currently accruing, no snapshot announced
- VAR expected Q4 2026, no exact date disclosed
- Funding $171.8M total: $110M Series B 18 Nov 2026 at $1.5B valuation,
  led by Bain Capital Crypto with Dragonfly, HSG, Peak XV, 50T Funds;
  $50M Series A led by Dragonfly May 2026; $10.3M seed Oct 2024.
  Also Coinbase Ventures, Hack VC, North Island, Mirana.
- Founders: Lucas V. Schuermann, Edward Yu
- Cost to farm: Arbitrum gas only — Omni charges zero trading fees
- **Hunter ranks this 11th at 57. That is the single worst ranking error.**

### Rolling, undated

**Miden** — `pre_token_rolling` — Ethereum ZK rollup
- 10% of supply planned, **primarily to holders and stakers of Polygon POL**;
  secondary route via testnet activity
- Snapshots began May 2025 and are ongoing. **No closing date exists.**
- Mainnet slipped from Q4 2025 to 2026. Reward date TBA.
- Funding $25M seed, 29 Apr 2025, led by a16z crypto, 1kx, Hack VC.
  Participants: Finality Capital, Symbolic Capital, P2 Ventures, Delta
  Fund, MH Ventures. Angels: Rune Christensen (MakerDAO), Avery Ching
  (Aptos), Sreeram Kannan (EigenLayer).
- Founders: Bobbin Threadbare, Dominik Schmid, Azeem Khan — former Meta
  blockchain engineers, spun out of Polygon Labs Agglayer Breakout
- **DEFECT: Hunter carries `deadline: "Snapshot closing"` as a literal
  string with `urgent: True` hardcoded. No such deadline exists.** The
  #1 rank and the briefing's instruction to bridge assets both rest on
  a typed phrase.
- **DEFECT: the AI briefing describes Miden as "a16z and Coinbase-backed".
  Coinbase is not in the round. This fabrication is on the live site.**

### Closed — unreachable

**Extended** — `pre_token_closed` — Arc
- XTEN: 300,000,000 tokens, 30% of 1B supply
- **Final eligibility snapshot completed 6 Nov 2026.** Nothing done now
  creates eligibility.
- Contract: `0x10DEDcBc9601aeC2A941EDbB4D13FcA7Fc820f38`
- Dates (all UTC): first batch distributed 29 Nov; **signing cutoff
  08:30 on 29 Nov**; spot trading opens 15:00 on 30 Nov; signing window
  closes 23:59 on 31 Dec, after which unsigned allocations go to the
  Ecosystem Fund
- Action if eligible: sign Community Allocation Terms in-app before the
  cutoff. Tokens do not arrive automatically.
- Constraint: staked XTEN and XTEN inside the 7-day unstaking period
  cannot be transferred. Vault depositors must stake for extra yield.
- Founded by former Revolut team members
- **DEFECT: Hunter had the deadline as date-only "2026-11-30". The real
  cutoff is 08:30 UTC on 29 Nov. Acting on Hunter would have missed the
  first batch by a day.**

### Token already live

**Morpho** — `token_live` — Ethereum
- MORPHO transferable since 21 Nov 2024. Max supply 1B. Market cap
  ~$1.29B. Listed on Binance, Kraken, Bybit, MEXC, Gate, KuCoin, Bitget.
- 4.9% already distributed to protocol users and launch pool participants
- Ongoing MORPHO rewards exist via liquidity provision in Morpho vaults,
  scaled by deposit size and duration — **capital-at-risk yield farming
  for a liquid token, not airdrop farming**
- **DEFECT: ranked 2nd at 70 in an airdrop tracker.**

**Aerodrome** — `token_live` — Base
- AERO has been live since 2023. Status needs the same write-up as Morpho.
- **UNVERIFIED — research outstanding**

### Retire

**Superposition** — `retire` — Aptos — **URGENT**
- Official account `@superp_fi` has posted nothing since 21 May 2025
  (eighteen months dormant)
- Users publicly reporting stuck Aptos lending positions, unable to repay
  or withdraw (15 Nov and 27 Nov 2026)
- No token, points or campaign signal
- **Remove today. Listing a protocol where users cannot withdraw is a
  liability, not a stale record.**

**Glider** — `retire`
- Official statement 19 Nov 2026: "We've never announced a token"
- Points frozen 11 Nov 2026; Discord roles snapshotted 19 Nov 2026
- Historical points converted to "Levels" — yield boosts and lower
  trading fees, explicitly **not** a token claim
- A revenue-share and referral programme remains open, but that is not
  an airdrop

**TreadFi** — `retire`
- No token, points, or snapshot announcement. Official posts in the
  14-day window are product-only. `@treadfoundation` silent.

**Farcaster** — `retire` or demote
- Official account posted only two podcast links in the 14-day window.
  No token, snapshot or eligibility language.
- Long-rumoured but nothing active. Keep only if we want a watchlist
  distinct from the tracked list.

### Unverified — research outstanding

Status unknown, must not be ranked until verified: **Base, Polymarket,
MetaMask, Ink (Kraken), Felix Protocol, Loopscale, Aerodrome.**

Known fragments: Base has no token and Jesse Pollak was reported on
21 Nov 2026 as saying they are "continuing to explore, but also not
being in a rush."

### Discovery candidates — not tracked, not verified

From the X scan, pre-token with current traction: **Tempo** (Stripe and
Paradigm incubated payments L1), **Arc** (Circle's L1, where XTEN is
issued), **Nado** (Kraken team on Ink; closed a points programme on
12 Nov 2026 with 33.2M points across 45,000+ users and no token
announced — textbook pre-TGE shape), **Predict.fun** (Binance-powered
prediction market, TGE postponed mid-Nov), **Multipli** (Pantera and
Coinbase Ventures, RWA lending).

Also previously found by discovery and never admitted: **Tydro**
($402.7M TVL on Ink, live airdrop points).

Nado and Tydro are the two most interesting. Neither is tracked.

## 5. Scoring model

Six factors, version 4: timing 25, tvl 20, investors 20, team 15,
onchain 15, social 5.

Live-measured: tvl, onchain, and timing where a real dated deadline
exists. Hand-assessed: investors, team, social. Confidence = live
factors / total, currently reaching 0.5.

Shown to the reader as **Measured / Partial / Unvalidated**, never as a
decimal — 0.5 means half the factors are measured, not a 50% probability
of anything. The old site showed a fabricated "snapshot probability"; do
not reintroduce any number that could be read as a likelihood.

`snapshotProb` still sits in the protocol records as dead data from that
feature. Remove it.

**Required additions (not yet built):**
- `entry_window`: state + dated close with time
- `eligibility_route`: how a new entrant qualifies, or that they cannot
- `class`: per section 3
- Dated deadlines must carry times. "30 November" cost us a day on
  Extended.
- Every event needs a source URL.

## 6. Scoring history

Schema 3.0 preserves, per scan, per protocol: id, name, score,
confidence, individual factors, factor sources, rank at observation,
eligibility/distributed status, scoring version, and a file-level
timestamp. Archive files are append-only and must never be overwritten —
this is what allows belief to be compared against outcome later.

Events are a separate append-only log. Lifecycle (announced → upcoming →
completed) is **derived at read time** from dates, never stored, so the
log keeps saying what was true when it was recorded. Lifecycle and
provenance are independent axes: `derived` means Hunter computed it,
which is sound; only an unconfirmed external report is flagged to the
reader.

Score movements across a scoring-model change are withheld rather than
presented as market activity. `MODEL_STABLE_SINCE = 2026-11-25` is the
backstop, because archive files written before schema 3.0 carry no
version and would otherwise read as "unchanged".

## 7. Known defects outstanding

1. Superposition listed while apparently abandoned, users unable to
   withdraw — **urgent**
2. Miden's phantom "Snapshot closing" deadline driving a #1 rank and a
   spend instruction
3. AI briefing fabricated a Coinbase investment in Miden
4. Morpho and Aerodrome ranked as airdrop opportunities with live tokens
5. Variational ranked 11th despite being the only confirmed open window
6. Glider, TreadFi, Farcaster tracked with no token announced
7. Deadlines stored as dates or free text, not timestamps
8. `snapshotProb` dead data still in records
9. Seven protocols never verified at all
10. `hunter_trajectory.json` is owned by root, not hunter — will fail a
    write from the timer

## 7a. Source tiers — safety critical

Eligibility criteria are the single most dangerous field Hunter carries,
because acting on a wrong one costs money and acting on a malicious one
costs the wallet.

Searching for "Polymarket airdrop eligibility" on 27 Nov 2026 returned,
as the top result, a site presenting fake live telemetry ("Eligible:
4,200 POLY tokens"), a fabricated 40% allocation, and a specific $50k
volume gate — for an airdrop that Polymarket's own help centre says has
never been announced. Any scraper would have ingested those numbers as
fact.

**Rule: eligibility criteria, allocations and deadlines may only be
sourced from the protocol's own domain or its verified account.**

| Tier | Source | Use |
|---|---|---|
| `official` | Protocol domain, verified account, official docs | May be published as fact |
| `reported` | Named person at the protocol, quoted in credible press | Publish as attributed claim, never as criteria |
| `rumour` | Aggregators, farming sites, unattributed | Never published. Discovery hint only. |

Hunter must carry a standing warning that no legitimate airdrop requires
signing a message or granting wallet access to check eligibility.

Polymarket is the worked example: the CMO confirmed a token and airdrop
in Oct 2025 (`reported`), the help pages still deny any announcement
(`official`), and the circulating criteria are `rumour`. Correct output
is "promised, not scheduled, no criteria published" — not a volume gate.

## 8. Working rules

- Read state from the server before asserting it. Every error in this
  build came from inferring state that was available to check.
- Verify a protocol has an airdrop before it is ranked. The list was
  never audited for this, which is the root cause of the rest.
- One claim, one source, with a date. Factors carry an assessment date so
  staleness is visible.
- Never publish a number that reads as a probability unless it is one.
- Changes to the engine go through a patcher that syntax-checks and
  writes to a new filename, or a whole-file swap guarded by an md5 of the
  file being replaced.
- Server modules are pulled by `curl` with checksums, never pushed
  through a browser download folder.
