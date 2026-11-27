# Hunter Intelligence — server modules

Python modules that run on the Hetzner box at
`/home/hunter/hunter-intelligence/`.

They live here so the server can pull them directly rather than being
sent through a browser download and an scp. Downloading by hand produced
`file (1).py`, `file (2).py` and a run against the wrong bytes more than
once; a checksum the server can verify against a known source removes
that whole class of problem.

## Pulling

On the server:

```bash
cd /home/hunter/hunter-intelligence
curl -sfO https://raw.githubusercontent.com/oi-ecosystem/airdrop-ecosystem/main/hunter/hunter_events.py
chown hunter:hunter hunter_events.py
```

Several at once:

```bash
cd /home/hunter/hunter-intelligence
for f in hunter_feed.py hunter_events.py build_trajectory.py; do
  curl -sfO "https://raw.githubusercontent.com/oi-ecosystem/airdrop-ecosystem/main/hunter/$f"
done
chown hunter:hunter *.py
```

## Verifying you have what you think you have

`CHECKSUMS` holds the md5 of each file as committed. After pulling:

```bash
curl -sf https://raw.githubusercontent.com/oi-ecosystem/airdrop-ecosystem/main/hunter/CHECKSUMS -o /tmp/ck
md5sum -c /tmp/ck 2>/dev/null | grep -v OK || echo "all files match"
```

Anything that does not print OK is not the file that was committed.

## What is here

| File | Purpose |
|---|---|
| `hunter_feed.py` | DefiLlama feed — TVL for protocols and chains, retirement detection by token symbol, market momentum, discovery and quarantine |
| `hunter_events.py` | Event log — distributions, deadlines, universe changes, material score moves. Append-only, with archive backfill |
| `build_trajectory.py` | Score history across the full briefings archive |

`hunter_app.py` is **not** here. It carries the hand-authored protocol
list, which is editorial content rather than code, and it is patched in
place on the server with a backup each time.

## No secrets

Nothing here holds a credential. `HUNTER_KEY` is read from the
environment, the DefiLlama endpoints are public and unauthenticated, and
the scoring factors these modules produce are already served publicly at
`rcloop-proxy.vercel.app/api/scores`.

If a module ever needs a secret, it reads it from the environment and
the value stays on the server.
