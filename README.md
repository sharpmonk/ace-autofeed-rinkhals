# ace-autofeed

A small workaround daemon for [Rinkhals](https://github.com/jbatonnet/Rinkhals)-equipped
Anycubic printers with the ACE Pro filament hub, fixing the bug where prints
don't auto-load filament before extrusion.

## What's the bug?

On a fresh Rinkhals install with an ACE Pro, when you start a print:

- The touchscreen runs the normal sequence: nozzle clean → bed level → preheat → Print
- ...but never issues `FEED_FILAMENT` to load the selected ACE slot into
  the nozzle
- If filament was previously retracted back to the ACE (which happens
  automatically after each print), the print starts with no filament at
  the nozzle — first layer extrudes nothing, print fails

Same bug if you start prints from Fluidd / Mainsail / USB / Orca-remote-send.
The single workflow that doesn't hit it: prints sliced in **Anycubic Slicer Next**
because that slicer embeds explicit `T0`/`T1`/`T2`/`T3` tool-change commands
which trigger Anycubic's separate tool-change handler.

If you use OrcaSlicer single-colour prints, you're affected.

Tracking upstream: [Rinkhals issue #XXX] (link goes here once filed).

## What this daemon does

Runs as a small background process on the printer. Watches Moonraker for
print state transitions. When a print starts:

1. Reads `print_stats.state` — if it just went to `printing`
2. Reads `filament_hub.current_filament` — if empty (nothing loaded)
3. Picks the slot to feed in this order:
   - `mmu.gate` if the touchscreen Color Match propagated it (rare — see #443)
   - First slot in the `.acm` sidecar's `ams_box_mapping`
   - First loaded slot in `filament_hub.slots[]`
4. Waits for the hotend to reach the actual print temperature (`target ≥ 190°C`
   with `temp` within 10°C of target) — this avoids triggering during LeviQ3's
   170°C/140°C probing routine
5. Issues `FEED_FILAMENT INDEX={slot} LENGTH=80 SPEED=25` via Moonraker

Cost: 1Hz HTTP polling against local Moonraker (~negligible CPU).

## Tested on

- Anycubic Kobra 3 Combo (ACE Pro + base printer)
- Rinkhals 20260501_01
- Stock firmware 2.4.6.7
- OrcaSlicer 2.3.2

Should work on any Rinkhals K3/KS1/K3M/K3V2/KS1M Combo (any printer with ACE).
If you test on another model please open an issue here so we can confirm.

## Install

Requires SSH access to the printer. Default Rinkhals credentials:
`root` / `rockchip`.

```bash
# From your dev machine:
ssh root@<printer-ip> "mkdir -p /useremain/home/rinkhals/ace-autofeed"
scp ace-autofeed.py root@<printer-ip>:/useremain/home/rinkhals/ace-autofeed/
ssh root@<printer-ip> "chmod +x /useremain/home/rinkhals/ace-autofeed/ace-autofeed.py"
```

## Start the daemon

```bash
ssh root@<printer-ip>
nohup python3 /useremain/home/rinkhals/ace-autofeed/ace-autofeed.py \
  --log-file /useremain/home/rinkhals/ace-autofeed/ace-autofeed.log \
  > /dev/null 2>&1 &
```

Verify it's running:
```bash
ps | grep ace-autofeed | grep -v grep
tail -f /useremain/home/rinkhals/ace-autofeed/ace-autofeed.log
```

## Test it

1. Ensure no filament loaded: touchscreen → Filament → Unload (or just verify
   `current_filament: ""` in Fluidd's mmu widget)
2. Send a print as you normally would (touchscreen Color Match → Print, or
   from Orca, or Fluidd Reprint)
3. Watch the daemon log:
   ```
   print started: filename='Cube_PLA_0.2_20m31s.gcode' current_filament=''
   slot pick: .acm ams_box_mapping[0].ams_index = 2
   will feed slot 2 once extruder is hot enough
   sending gcode: FEED_FILAMENT INDEX=2 LENGTH=80 SPEED=25
   gcode acknowledged
   ```
4. Print should now extrude normally.

## Sending direct from OrcaSlicer — pre-select the slot

When you upload-and-print from Orca (Print Plate → Send to printer & Print)
the daemon will catch the print and feed automatically. **However**, the
slot it picks depends on the priority chain in `pick_slot()`:

1. `mmu.gate` (if ≥ 0)
2. `.acm` sidecar's first `ams_box_mapping[].ams_index`
3. First ready slot in `filament_hub.slots[]`

`mmu.gate` is **persistent** — it carries over from the previous print
(or boot default `-1`). So if your last print used slot 3 but you sliced
this one in Orca for slot 2, the daemon will still pick slot 3.

**Workaround**: before hitting Print in Orca, open Fluidd or Mainsail's
**MMU widget** and click the slot you actually want loaded. That sets
`mmu.gate` to the right value, so the daemon's first-priority lookup
matches your intent.

(Touchscreen Color Match → Print does this for you automatically — that's
why prints started from the screen don't hit this gotcha.)

## Auto-start at boot

Use [`install.sh`](./install.sh) — it sets up a proper Rinkhals user app
that survives reboots AND Rinkhals upgrades:

```bash
ssh root@<printer-ip> 'curl -fsSL https://raw.githubusercontent.com/sharpmonk/ace-autofeed-rinkhals/main/install.sh | sh'
```

This creates `/useremain/home/rinkhals/apps/99-ace-autofeed/` with the
files Rinkhals's app loader expects:

```
99-ace-autofeed/
├── app.sh        # start | stop | status subcommands (called by Rinkhals on boot)
├── app.json      # metadata: $version, name, description
└── .enabled      # marker file — without this, the app won't auto-start
```

Manual control if needed:
```bash
sh /useremain/home/rinkhals/apps/99-ace-autofeed/app.sh status
sh /useremain/home/rinkhals/apps/99-ace-autofeed/app.sh stop
sh /useremain/home/rinkhals/apps/99-ace-autofeed/app.sh start
```

> **Note:** earlier versions of this README pointed to `/useremain/rinkhals/apps/`
> with `start.sh`/`stop.sh` scripts. That path is **wrong** — Rinkhals's app
> loader looks at `/useremain/home/rinkhals/apps/` (`USER_APP_PATH`) and only
> picks up apps with `app.sh` + `app.json` + `.enabled`. The current
> `install.sh` migrates from the legacy layout automatically.

## Uninstall

```bash
ssh root@<printer-ip>
sh /useremain/home/rinkhals/apps/99-ace-autofeed/app.sh stop
rm -rf /useremain/home/rinkhals/ace-autofeed
rm -rf /useremain/home/rinkhals/apps/99-ace-autofeed
# Legacy cleanup — only needed if you ever installed an older version:
rm -rf /useremain/rinkhals/apps/99-ace-autofeed 2>/dev/null
```

## Caveats / known limitations

### Slot picking
- The daemon assumes the FIRST slot in the `.acm` sidecar's `ams_box_mapping`
  is the one you want to feed. For single-colour prints this is correct.
  For multi-colour prints the slicer-generated `Tn` commands handle tool
  selection naturally — the daemon's initial feed gets superseded.
- If the touchscreen's Color Match propagates `mmu.gate` correctly
  (Rinkhals issue #443 — fixed in some releases?), the daemon uses that
  signal preferentially over the .acm fallback.

### Feed timing
- Daemon waits up to 10 minutes for `target ≥ 190` before giving up.
  A very long LeviQ3 routine could in theory exceed this, in which case the
  feed never fires. Hasn't happened in practice on a 5×5 mesh.

### Bed leveling on GoKlipper (confirmed stubs)
The following standard Klipper commands are **non-functional on Anycubic's
GoKlipper** (confirmed on Kobra 3 Combo, Rinkhals 20260501_01):

| Command | Behavior |
|---|---|
| `BED_MESH_CALIBRATE` | Heats, wipes, stops at first probe point. No probe motion, no error. |
| `BED_MESH_CLEAR` | No-op. State and `printer_mutable.cfg` unchanged. |
| `BED_MESH_PROFILE REMOVE=default` | No-op. |
| `RESPOND TYPE=command MSG="..."` | Command accepted but output never forwarded as a response event, so Fluidd/Mainsail `action:prompt_*` popups don't appear. |

The only working bed-level command on GoKlipper is **`LEVIQ`**, which:
- On a cold boot, does a full 25-point probe internally
- On warm/subsequent calls, short-circuits to a center-only probe + Z-offset
- Updates `[probe] z_offset` in `printer_mutable.cfg` but does NOT persist a
  fresh `bed_mesh default` profile

The active bed mesh is whichever was last saved by **touchscreen
"Auto-leveling"** — that path uses Anycubic's internal MQTT auto-level
which DOES persist a fresh 25-point mesh to `printer_mutable.cfg`, and
that mesh applies to all subsequent prints from any source (touchscreen,
Mainsail, Fluidd, Orca direct).

**Practical workflow:** when you want a fresh mesh, press "Auto Leveling"
on the touchscreen (~5 min). Daily prints use the saved mesh + LEVIQ
Z-offset re-cal automatically.

References:
- Rinkhals issue [#400](https://github.com/jbatonnet/Rinkhals/issues/400)
  ("ABL only proves 1 point in the center") — collaborator confirms LAN
  print path skips MQTT auto-leveling, BMC is center-only "by design"
- Rinkhals issue [#321](https://github.com/jbatonnet/Rinkhals/issues/321)
  — silent console, LEVIQ Z-offset drift, related symptoms

### LEVIQ injection
The daemon injects `LEVIQ` at the top of every uploaded gcode file. This
is largely **redundant** because Anycubic's firmware auto-calls LEVIQ
before SDCARD prints anyway — but the injection is harmless (a second
LEVIQ short-circuits to no-op) and protects against firmware variants
that might skip it.

### This is a workaround
The proper fixes are upstream in GoKlipper. Once Anycubic implements
working `BED_MESH_*` commands and forwards `RESPOND` output, this
daemon becomes much less interesting.

## CLI flags

```
--verbose, -v    DEBUG-level logging (polls visible every 1s)
--dry-run        Log what it would send but don't actually call FEED_FILAMENT
--no-inject      Disable LEVIQ injection (only auto-feed runs)
--log-file PATH  Mirror logs to a file (with rotation: 500KB × 3 backups)
```

## Diagnostic: confirm you're affected before installing

If you're not sure this bug affects your setup, run this on the printer
right after a failed print (where the first layer extruded nothing):

```bash
curl -s 'http://127.0.0.1:7125/printer/objects/query?filament_hub=current_filament&mmu=gate'
```

If you see `current_filament: ""` and `gate: -1` despite having just
selected a slot via Color Match → this is exactly the bug.

## How it talks to the printer

All via the standard Moonraker HTTP API on `localhost:7125`. No fiddling
with `printer.cfg`, no risk of Klipper boot failure (error 11407). Easy
to fully disable / uninstall.

## License

[MIT recommended — keep it simple]

## Contributions

Issues, PRs welcome. Especially:
- Testing on other Anycubic + ACE models (K3M Combo, KS1 Combo, K3V2 Combo)
- Better slot-detection logic (especially for multi-colour where slots may
  load mid-print)
- Native Rinkhals integration (PR'd into Rinkhals itself, replacing this
  external daemon)
