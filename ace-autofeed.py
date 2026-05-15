#!/usr/bin/env python3
"""
ace-autofeed
============
Auto-trigger FEED_FILAMENT (and optionally inject LEVIQ) when a print
starts on a Rinkhals-equipped Anycubic Kobra 3 Combo. Works around two
upstream bugs:

  1. The missing auto-load step in the Color Match -> Print flow (and
     SDCARD_PRINT_FILE from any source) — fixed by FEED_FILAMENT injection.
  2. Orca-direct prints skipping LeviQ3 bed level — partially fixed by
     prepending `LEVIQ` at the top of uploaded gcode files (firmware
     auto-calls LEVIQ before SDCARD prints anyway, so this is largely
     redundant — but harmless and protects against firmware variants that
     skip it). For a fresh 25-point mesh, invoke the FRESH_MESH macro
     manually from Fluidd when the bed is clear; per-phase sequence works
     in standby state but firmware short-circuits LEVIQ_PROBE during
     prints regardless of how it's invoked (see v9 → v10 reversal note
     in inject_leviq).

Behaviour:
  Print-start watcher (main thread):
    - Poll Moonraker every POLL_INTERVAL seconds
    - On state transition to 'printing':
        - Skip if filament_hub.current_filament is non-empty (already loaded)
        - Determine slot via mmu.gate -> .acm sidecar -> first ready slot
        - Wait for hotend at print temp (target >= 190, temp within 10°C)
        - Send FEED_FILAMENT INDEX={slot} LENGTH=80 SPEED=25
    - One trigger per print (reset on state leave)

  File-scan watcher (background thread):
    - Scan GCODE_DIR every FILE_SCAN_INTERVAL seconds
    - For each new .gcode file (mtime >= 3s old to avoid racing gkapi):
        - If file's first LEVIQ_SCAN_LINES already contain LEVIQ or our
          injection marker, skip
        - Otherwise atomically prepend `LEVIQ` + marker comment so the
          print self-levels before its own start_gcode runs

Usage:
  python3 ace-autofeed.py             foreground, INFO logging
  python3 ace-autofeed.py --verbose   DEBUG logging
  python3 ace-autofeed.py --dry-run   log gcode + injections, don't apply
  python3 ace-autofeed.py --no-inject disable LEVIQ injection thread

Place at: /useremain/home/rinkhals/ace-autofeed/ace-autofeed.py
Log:      /useremain/home/rinkhals/ace-autofeed/ace-autofeed.log
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
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

# LEVIQ injection
GCODE_DIR = "/useremain/app/gk/gcodes"
LEVIQ_SCAN_LINES = 200      # how many lines from the start to scan for LEVIQ
FILE_SCAN_INTERVAL = 5.0    # seconds between gcode dir scans
FILE_STABILITY_SECONDS = 3  # require mtime older than this before patching
INJECTION_MARKER = "; ace-autofeed: LEVIQ injected"


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
    acm_path = f"{GCODE_DIR}/{base}.acm"
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


def gcode_has_leviq(filepath, log):
    """Return True if file's first N lines contain LEVIQ or our injection marker."""
    try:
        with open(filepath, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= LEVIQ_SCAN_LINES:
                    break
                if INJECTION_MARKER in line:
                    return True
                # Strip inline comments and uppercase for case-insensitive match
                stripped = line.split(";", 1)[0].strip().upper()
                if (stripped == "LEVIQ"
                        or stripped.startswith("LEVIQ ")
                        or stripped.startswith("LEVIQ_")):
                    return True
        return False
    except Exception as e:
        log.warning(f"failed to scan {filepath}: {e}")
        # Err on the side of not modifying — pretend it's already there
        return True


def inject_leviq(filepath, log):
    """Atomically prepend LEVIQ + marker to a gcode file.

    Note: we deliberately do NOT inject BED_MESH_CALIBRATE — confirmed broken
    on GoKlipper (stops at first probe point, no error). LEVIQ on a cold
    printer probes the full 5x5 mesh internally; on subsequent calls within
    its caching window it short-circuits to a center probe + Z-offset only.
    The saved bed mesh profile remains loaded either way, so bed-shape
    compensation always applies.

    v9 attempted to inject the per-phase sequence (LEVIQ_AUTO_ZOFFSET_ON_OFF /
    PREHEATING / WIPING / PROBE / SAVE_CONFIG) to force a fresh mesh, but
    confirmed via gklib log analysis 2026-05-15 that GoKlipper has separate
    code paths for LEVIQ_PROBE based on print_stats.state — "printing" always
    short-circuits to center-only regardless of which macro is invoked. The
    per-phase injection just wasted ~5 min per print without producing a
    fresh mesh. Reverted in v10.

    For an on-demand fresh mesh from Fluidd, use the FRESH_MESH gcode_macro
    defined in printer.custom.cfg (calls the same per-phase sequence — works
    because the printer is in standby state when invoked manually).
    """
    tmp = filepath + ".ace-autofeed-tmp"
    header = f"{INJECTION_MARKER}\nLEVIQ\n"
    try:
        with open(filepath, "rb") as src, open(tmp, "wb") as dst:
            dst.write(header.encode())
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(tmp, filepath)
        log.info(f"injected LeviQ sequence into {os.path.basename(filepath)}")
        return True
    except Exception as e:
        log.warning(f"failed to inject LEVIQ into {filepath}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def file_scanner(log, dry_run, stop_event):
    """Background thread: patch new .gcode files lacking LEVIQ."""
    seen = set()  # (filename, mtime) tuples we've already processed
    log.info(f"file scanner started (dir={GCODE_DIR}, interval={FILE_SCAN_INTERVAL}s)")
    while not stop_event.is_set():
        try:
            entries = os.listdir(GCODE_DIR)
        except FileNotFoundError:
            entries = []
        except Exception as e:
            log.warning(f"file scanner listdir error: {e}")
            entries = []

        now = time.time()
        for fn in entries:
            if not fn.endswith(".gcode"):
                continue
            fpath = os.path.join(GCODE_DIR, fn)
            try:
                if not os.path.isfile(fpath):
                    continue
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue

            # Wait for the file to "settle" — gkapi parses metadata and writes
            # the .acm shortly after upload. We don't want to race that.
            if now - mtime < FILE_STABILITY_SECONDS:
                log.debug(f"{fn}: too fresh ({now - mtime:.1f}s), deferring")
                continue

            key = (fn, mtime)
            if key in seen:
                continue
            seen.add(key)

            if gcode_has_leviq(fpath, log):
                log.debug(f"{fn}: LEVIQ already present, skipping")
                continue

            log.info(f"{fn}: no LEVIQ found, will inject")
            if dry_run:
                log.info(f"DRY RUN: would inject LEVIQ into {fn}")
            else:
                inject_leviq(fpath, log)
                # The injection updates mtime — re-record so we don't re-scan
                try:
                    seen.add((fn, os.path.getmtime(fpath)))
                except OSError:
                    pass

        # Bounded sleep so the thread shuts down quickly on signal
        for _ in range(int(FILE_SCAN_INTERVAL * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-inject", action="store_true",
                        help="disable LEVIQ injection thread (only auto-feed runs)")
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
    log.info(
        f"ace-autofeed v10 started (dry_run={args.dry_run}, "
        f"poll={POLL_INTERVAL}s, wait_target>={FEED_TARGET_MIN}, "
        f"inject_leviq={not args.no_inject})"
    )

    stop_event = threading.Event()
    scanner_thread = None
    if not args.no_inject:
        scanner_thread = threading.Thread(
            target=file_scanner,
            args=(log, args.dry_run, stop_event),
            name="leviq-scanner",
            daemon=True,
        )
        scanner_thread.start()

    last_state = None
    fed_for_this_print = False
    try:
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
                    fed_for_this_print = False

                last_state = state
            except Exception as e:
                log.warning(f"main poll error: {e}")

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        stop_event.set()
        if scanner_thread is not None:
            scanner_thread.join(timeout=2)


if __name__ == "__main__":
    main()
