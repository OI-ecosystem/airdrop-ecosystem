# Hunter server modules — pull route

The server is the source of truth. These files are published here so the
server can pull them with `curl` instead of files being pushed through a
browser's download folder, where same-named files collide silently and
you end up running `hunter_events(3).py`.

## Pull

    cd /home/hunter/hunter-intelligence
    BASE=https://raw.githubusercontent.com/oi-ecosystem/airdrop-ecosystem/main/hunter

    # Back up first — hunter_app.py is replaced whole, not patched
    for f in hunter_app.py hunter_events.py build_newsletter.py; do
      [ -f "$f" ] && cp "$f" "$f.bak-$(date +%F-%H%M)"
    done

    for f in hunter_app.py hunter_events.py hunter_feed.py \
             build_newsletter.py build_trajectory.py; do
      curl -fsSL "$BASE/$f" -o "$f"
    done

    curl -fsSL "$BASE/CHECKSUMS" -o /tmp/CHECKSUMS
    md5sum -c /tmp/CHECKSUMS

Every line must say `OK`. If any says `FAILED`, restore the `.bak-*` file
and stop — a half-applied update is worse than an old one.

## Before you pull: the safety check

`hunter_app.py` is replaced in full this time, because the change touches
the output assembly and a patcher would be more fragile than a swap. So
confirm the file you are replacing is the one this version was built
from:

    md5sum hunter_app.py

Expected: `6c7b5bcf6101d721b59f3bcbecd202bc`

If it matches, the swap is safe. If it does not, the server has changed
since this build — stop and say so, because overwriting would discard
whatever changed.

## After pulling (27 Nov audit build)

The protocol list was rebuilt on 27 Nov 2026. Nine protocols were retired
and classes were introduced, so the event log must be rebuilt once — the
old log contains deadline and score events derived from records that have
since been corrected.

## After pulling

    sudo -u hunter python3 -c "import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]]" \
      hunter_app.py hunter_events.py build_newsletter.py

    # Rebuild the event log. Required: the audit changed what counts as
    # an event, and PROTOCOL_LIST_VERSION now suppresses movements caused
    # by our own edits rather than reporting them as market activity.
    sudo -u hunter python3 hunter_events.py --reset --backfill

    # hunter_trajectory.json is owned by root and will fail a write from
    # the timer, which runs as hunter
    sudo chown hunter:hunter hunter_trajectory.json

    # Build this week's issue, then run a scan to publish everything
    sudo -u hunter python3 build_newsletter.py
    sudo -u hunter python3 hunter_app.py --scan

The scan now carries events, score history and last week's issue in the
same `/api/scores` payload, so the website needs no new endpoint.
