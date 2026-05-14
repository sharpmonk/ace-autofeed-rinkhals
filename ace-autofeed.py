#!/usr/bin/env python3
"""
ace-autofeed
============
Auto-trigger FEED_FILAMENT when a print starts on a Rinkhals-equipped
Anycubic Kobra 3 Combo. Works around the missing auto-load step in the
Color Match -> Print flow (and SDCARD_PRINT_FILE from any source).

Behaviour:
  - Poll Moonraker every POLL_INTERVAL seconds
  - When print_stats.state transitions to 'printing':
      - Skip if filament_hub.current_filament is non-empty (already loaded)
      - Determine the desired slot:
          1. mmu.gate if >= 0 (Color Match propagated to MMU state)
          2. .acm sidecar's first ams_box_mapping[].ams_index
          3. First loaded slot from filament_hub.slots[]
      - Enter a sub-loop that waits for the extruder to reach the feed
        threshold (configurable; default = max(170, target - 10))
      - When hot enough AND state is still 'printing' AND cf is still '':
          - Send FEED_FILAMENT INDEX={slot} LENGTH=80 SPEED=25
      - Bail out if:
          - state leaves 'printing' (cancelled/paused) — abort this attempt
          - filament_hub.current_filament becomes non-empty — already loaded
          - wait exceeds MAX_FEED_WAIT seconds — give up to avoid stuck state
  - Single trigger per print (mark _fed_for_print and reset on state leave)

Usage:
  python3 ace-autofeed.py             (foreground, INFO logging)
  python3 ace-autofeed.py --verbose   (DEBUG logging)
  python3 ace-autofeed.py --dry-run   (log what it would send, don't send)

Place at: /useremain/home/rinkhals/ace-autofeed/ace-autofeed.py
Log:      /useremain/home/rinkhals/ace-autofeed/ace-autofeed.log
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.parse
import urllib.request

API = "http://127.0.0.1:7125"
POLL_INTERVAL = 1.0
FEED_LENGTH = 80
FEED_SPEED = 25
HTTP_TIMEOUT = 5
FEED_TIMEOUT = 60

# Wait conditions before feed
# We must wait through LeviQ3's probing routine (which alternates between
# 170°C heat and 140°C cool — extru_temp / extru_end_temp). Only when the
# gcode finishes leveling and sets target to the actual print temperature
# (e.g. M104 S230) do we know it's the right moment to feed.
FEED_TARGET_MIN = 190     # target must be at least this (signals "print phase", not LeviQ3)
FEED_TEMP_MARGIN = 10     # temp must be within this of target (heating is complete)
MAX_FEED_WAIT = 600       # 10 min — covers a 5x5 bed mesh + heat-up


def query(objects):
    pieces = []
    for name, attrs in objects.items():
        if isinstance(attrs, list):
            attrs = ",".join(attrs)
        pieces.append(f"{name}={attrs}" if attrs else name)
    url = f"{API}/printer/objects/query?{'&'.join(pieces)}"
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())["result"]["status"]


def gcode(cmd, log):
    url = f"{API}/printer/gcode/script?script={urllib.parse.quote(cmd)}"
    log.info(f"sending gcode: {cmd}")
    try:
        with urllib.request.urlopen(url, timeout=FEED_TIMEOUT) as r:
            r.read()
        log.info("gcode acknowledged")
        return True
    except Exception as e:
        log.warning(f"gcode send failed: {e}")
        return False


def find_acm_for(filename, log):
    if not filename:
        return None
    base = os.path.basename(filename)
    if base.endswith(".gcode"):
        base = base[:-len(".gcode")]
    elif "." in base:
        base = base.rsplit(".", 1)[0]
    acm_path = f"/useremain/app/gk/gcodes/{base}.acm"
    if not os.path.isfile(acm_path):
        log.debug(f".acm not found at {acm_path}")
        return None
    try:
        with open(acm_path) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"failed to read {acm_path}: {e}")
        return None


def pick_slot(state, log):
    gate = state.get("mmu", {}).get("gate", -1)
    if isinstance(gate, int) and gate >= 0:
        log.debug(f"slot pick: mmu.gate = {gate}")
        return gate

    filename = state.get("print_stats", {}).get("filename", "")
    acm = find_acm_for(filename, log)
    if acm and acm.get("use_ams") and acm.get("ams_box_mapping"):
        idx = acm["ams_box_mapping"][0].get("ams_index")
        if isinstance(idx, int) and idx >= 0:
            log.debug(f"slot pick: .acm ams_box_mapping[0].ams_index = {idx}")
            return idx

    hub = state.get("filament_hub", {})
    for h in hub.get("filament_hubs", []) or []:
        for s in h.get("slots", []) or []:
            if s.get("status") == "ready":
                idx = s.get("index")
                if isinstance(idx, int) and idx >= 0:
                    log.debug(f"slot pick: filament_hub first ready slot = {idx}")
                    return idx

    log.debug("slot pick: no candidate")
    return None


def attempt_feed(slot, log, dry_run):
    """Wait until conditions are right, then send FEED_FILAMENT once."""
    start = time.time()
    while time.time() - start < MAX_FEED_WAIT:
        try:
            s = query({
                "print_stats": "state",
                "filament_hub": "current_filament",
                "extruder": ["temperature", "target"],
            })
            state = s.get("print_stats", {}).get("state")
            cf = s.get("filament_hub", {}).get("current_filament", "")
            ext = s.get("extruder", {})
            temp = float(ext.get("temperature", 0) or 0)
            target = float(ext.get("target", 0) or 0)

            log.debug(f"feed wait: state={state} cf={cf!r} temp={temp:.1f}/{target:.1f}")

            if state != "printing":
                log.info(f"print no longer 'printing' (state={state}), aborting feed")
                return
            if cf:
                log.info(f"filament loaded externally as {cf!r}, aborting feed")
                return

            # Two-condition gate:
            #   1. target >= FEED_TARGET_MIN  → gcode has left LeviQ3 (170/140 range)
            #                                    and is now setting the real print temp
            #   2. temp >= target - margin    → heat-up to that target is essentially done
            ready = target >= FEED_TARGET_MIN and temp >= (target - FEED_TEMP_MARGIN)
            if ready:
                cmd = f"FEED_FILAMENT INDEX={slot} LENGTH={FEED_LENGTH} SPEED={FEED_SPEED}"
                if dry_run:
                    log.info(f"DRY RUN: would send: {cmd}")
                else:
                    if gcode(cmd, log):
                        return
                    log.info("feed failed, will retry in 3s")
                    time.sleep(3)
                    continue
                return

        except Exception as e:
            log.warning(f"feed-wait poll error: {e}")
        time.sleep(2)
    log.warning(f"gave up after {MAX_FEED_WAIT}s waiting to feed slot {slot}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args()

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        # Rotate at 500KB, keep 3 backups → max ~2MB disk used
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log_file,
            maxBytes=500_000,
            backupCount=3,
        ))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("ace-autofeed")
    log.info(f"ace-autofeed v5 started (dry_run={args.dry_run}, poll={POLL_INTERVAL}s, wait_target>={FEED_TARGET_MIN})")

    last_state = None
    fed_for_this_print = False
    while True:
        try:
            status = query({
                "print_stats": ["state", "filename"],
                "filament_hub": ["current_filament", "filament_hubs"],
                "mmu": "gate",
            })
            state = status.get("print_stats", {}).get("state")
            cf = status.get("filament_hub", {}).get("current_filament", "")

            log.debug(f"main: state={state} current_filament={cf!r}")

            if state == "printing" and last_state != "printing":
                fname = status.get("print_stats", {}).get("filename", "")
                log.info(f"print started: filename={fname!r} current_filament={cf!r}")

                if cf:
                    log.info(f"no auto-feed needed (filament already loaded as {cf!r})")
                    fed_for_this_print = True
                else:
                    slot = pick_slot(status, log)
                    if slot is None:
                        log.warning("no slot candidate found — skipping auto-feed")
                    else:
                        log.info(f"will feed slot {slot} once extruder is hot enough")
                        attempt_feed(slot, log, args.dry_run)
                        fed_for_this_print = True
            elif state != "printing" and last_state == "printing":
                # print ended — reset for next one
                fed_for_this_print = False

            last_state = state
        except KeyboardInterrupt:
            log.info("interrupted, exiting")
            return
        except Exception as e:
            log.warning(f"main poll error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
