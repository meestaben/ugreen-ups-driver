# UGREEN US3000 UPS — Custom NUT Driver

Custom Python NUT driver for the UGREEN US3000 UPS running with TrueNAS SCALE (tested on a UGREEN DXP4800 Plus), replacing the generic `usbhid-ups` integration which reports whether the device is on battery or not but not much else.

## Repository Contents

| File | Description |
|------|-------------|
| `ugreen_ups_driver.py` | Python NUT driver — reads HID raw device, serves NUT protocol |
| `install-ugreen-ups.sh` | Installer/uninstaller — idempotent, configures everything via TrueNAS middleware API |

The installer generates these files at install time (not committed — contain templated paths):
- `find-and-bind-ups.sh` — locates USB device by VID/PID and binds hidraw
- `ugreen-ups-driver.service` — systemd service unit
- `ugreen-ups-init.sh` — TrueNAS post-init script (re-registers service and patches driver list after updates)

---

## Background

**Hardware:** UGREEN DXP4800 Plus NAS with US3000 UPS (USB VID:`2b89` PID:`ffff`)

**Problem:** TrueNAS SCALE ships NUT 2.8.0. The built-in `usbhid-ups` driver with `subdriver=explore` reports incorrect or missing data for this device. The US3000 uses a vendor-defined HID page `0xFF00`, report `0x71`, rather than standard HID Power Device pages.

**Solution:** A pure Python driver that reads `/dev/hidrawN` directly via `HIDIOCGFEATURE` ioctl and interrupt stream, decodes the proprietary byte map, and serves a standard NUT protocol on a secondary port (3494). TrueNAS NUT is configured with `dummy-ups` pointing at `localhost:3494`, which proxies the data into the TrueNAS UPS subsystem (graphs, alerts, upsmon shutdown).

---

## USB / HID Architecture

The device presents as a USB HID device. TrueNAS NUT normally claims it via `usbfs` (libusb). To use `hidraw` instead:

1. Stop `nut-driver@ugreen` (releases the usbfs claim)
2. Bind interface `{USB_DEV}:1.0` to `usbhid` kernel driver
3. `/dev/hidrawN` appears — driver opens it directly

`find-and-bind-ups.sh` handles this at boot, scanning sysfs by VID/PID so it works regardless of USB port or bus assignment.

---

## Firmware

This has only been tested with US3000 firmware **V3.3**

## HID Report Byte Map (V3.3 firmware, partially validated by live testing)

### Stream Report `0x71`

**`byte[7]` appears to be the authoritative status byte:**

| `byte[7]` | Mode | NUT `ups.status` |
|-----------|------|-----------------|
| `0x26` | On mains, battery full | `OL` |
| `0x36` | On mains, battery charging | `OL CHRG` |
| `0x21` | On battery (mains lost) | `OB` |

**Device note:** The US3000 is a DC-only UPS — input is 12V/19V/20V DC from a power brick; output is 12V DC to the NAS.

Confidence key: **CONFIRMED** = very likely correct basedon OL<->OB transition capture; **PLAUSIBLE** = value in expected range but not cross-verified; **ASSUMED** = extrapolated from another mode, not directly captured.

#### All modes — fields present regardless of mode
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[22-23]` | BE u16 ÷ 1000 V | `battery.voltage` (~16.4V full, 4S Li-ion) | CONFIRMED |
| `[28]` | raw byte °C | `ups.temperature` (42-57°C internal sensor) | PLAUSIBLE |
| `[30]` | raw byte % | `ups.load` (~12-15% idle) | CONFIRMED |
| `[35-36]` | BE u16 ÷ 1000 V | `battery.cell.1.voltage` (~4.108V full) | CONFIRMED |
| `[37-38]` | BE u16 ÷ 1000 V | `battery.cell.2.voltage` | CONFIRMED |
| `[39-40]` | BE u16 ÷ 1000 V | `battery.cell.3.voltage` | CONFIRMED |
| `[41-42]` | BE u16 ÷ 1000 V | `battery.cell.4.voltage` | CONFIRMED |
| `[43]` | raw byte % | `battery.charge` | CONFIRMED |

Cell voltages sum to ≈ `battery.voltage`, confirming a 4S pack. `battery.voltage.nominal` is set to `16` reflecting actual HID measurements.

**Note on `[18-19]`:** This field measures different physical nodes depending on mode. In OL it reads the power brick input; in OB it reads the regulated 12V output to the NAS. Published as different NUT variables accordingly — see mode sections below.

#### OL mode (`0x26`) additional fields
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[18-19]` | BE u16 ÷ 1000 V | `input.voltage` (~18.8V DC from 19V power brick under load) | CONFIRMED |
| `[24-25]` | BE u16 ÷ 1000 A | `input.current` (~2.3A; 19V × 2.3A ≈ 43W = NAS + charging) | PLAUSIBLE |

Bytes `[16-17]` are a packet counter in OL mode. Bytes `[32-33]` previously misidentified as `input.current ÷ 100` — superseded by `[24-25] ÷ 1000`.

#### OL CHRG mode (`0x36`) additional fields
Same as OL mode. Layout not directly captured; extrapolated from OL analysis.

#### OB mode (`0x21`) additional fields
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[16-17]` | BE u16 seconds | `battery.runtime` | CONFIRMED |
| `[18-19]` | BE u16 ÷ 1000 V | `output.voltage` (~12.0V DC regulated output to NAS) | CONFIRMED |
| `[24-25]` | BE u16 ÷ 1000 A | `battery.current` (discharge, ~3.5-3.7A) | PLAUSIBLE |

`battery.runtime` starts high (~7600s) then rapidly re-estimates as BMS calculates from actual load; feature report `0x09` may give a different/lagging value.

### Feature Reports
| Report | Bytes | NUT Variable | Notes |
|--------|-------|--------------|-------|
| `0x06` | `[1]` 0-100% | `battery.charge` | BMS SOC — used as fallback/cross-check alongside stream `[43]` |
| `0x09` | `[1-4]` LE u32 | `battery.runtime` | `0xFFFFFFFF` = N/A (on mains); may lag stream `[16-17]` after mains loss |
| `0x13` | `[1-2]` LE u16 × 4 = Wh | `battery.capacity` | 264 × 4 = **1056 Wh** |

### Notes
- `ups.status` debounce: 3 consecutive matching reads (~3s) required before publishing a change
- `battery.cell.*.voltage` are non-standard NUT variables; published for cell balance monitoring
- Previous byte map had `battery.voltage` at `[20-21] ÷ 100` (~30V — wrong), `battery.runtime` at `[22-23]` in OB (wrong), temperature at `[34],[36],[38],[40]` (~13°C — wrong), `input.voltage` interpreted as 240V AC (wrong — DC-only device). All corrected from OL<->OB transition capture analysis.

---

## Driver Design

- Pure Python, no external dependencies beyond stdlib
- Reads hidraw via `HIDIOCGFEATURE` ioctl + `select()`/`read()` for interrupt stream
- Serves NUT protocol on configurable port (default `3494`)
- Auto-detects hidraw node by scanning `/sys/class/hidraw/*/device/uevent` for VID `2B89`
- Stream polling every 1s, feature report polling every 10s
- Variable deletion: `None` value removes stale fields when mode changes
- Asymmetric debounce on status transitions (see above)

**Usage:**
```bash
python3 ugreen_ups_driver.py --port 3494
upsc ugreen@localhost:3494   # via TrueNAS NUT relay
```

---

## TrueNAS Integration

### NUT configuration (auto-generated by TrueNAS, do not edit manually)

`/etc/nut/ups.conf`:
```
[ugreen]
    driver = dummy-ups
    port = ugreen@localhost:3494
    desc = "UGREEN US3000"
```

`/etc/nut/nut.conf`: `MODE=netserver`

### driver.list entry (wiped by TrueNAS updates — init script re-patches on boot)
```
"UGREEN"	"ups"	"1"	"US3000"	""	"dummy-ups"
```

### Confirmed middleware API syntax
```bash
# Configure UPS (install)
midclt call ups.update '{"driver": "dummy-ups$US3000", "port": "ugreen@localhost:3494", ...}'

# Revert UPS config to factory defaults (uninstall)
# Must use SLAVE mode — driver field is required when mode=MASTER, causing validation failure
midclt call ups.update '{
    "mode": "SLAVE",
    "remotehost": "localhost",
    "driver": "",
    "port": "",
    "identifier": "ups",
    "description": "",
    "monpwd": "fixmepass",
    "monuser": "upsmon",
    "shutdown": "LOWBATT",
    "shutdowntimer": 30,
    "hostsync": 15,
    "powerdown": false,
    "rmonitor": false,
    "options": "",
    "optionsupsd": "",
    "extrausers": ""
}'

# Get numeric service ID (required for enable/disable)
midclt call service.query '[]' | python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s.get('service') == 'ups': print(s['id']); break"

# Enable start on boot (use numeric ID, not name)
midclt call service.update '14' '{"enable": true}'

# Start / stop
midclt call service.start '"ups"' '{}'
midclt call service.stop '"ups"' '{}'

# Register init script
midclt call initshutdownscript.create '{"type": "SCRIPT", "script": "/path/to/script.sh", "when": "POSTINIT", "enabled": true, "comment": "..."}'

# Query init scripts
midclt call initshutdownscript.query '[["script", "=", "/path/to/script.sh"]]'

# Delete init script
midclt call initshutdownscript.delete '42'
```

---

## Installation

```bash
# Clone repo, then from the repo directory:
chmod +x install-ugreen-ups.sh
./install-ugreen-ups.sh install [/mnt/tank/system/ugreen-ups]

# Uninstall (reverts to OEM usbhid-ups, stops UPS service)
./install-ugreen-ups.sh uninstall [/mnt/tank/system/ugreen-ups]
```

The installer:
1. Copies driver files to install path (must be under `/mnt`)
2. Writes `find-and-bind-ups.sh`, systemd service, and init script (all path-templated)
3. Patches `dummy-ups` entry into `/usr/share/nut/driver.list`
4. Generates a random upsmon password (stored at `$INSTALL_PATH/.upsmon-password`, mode 600)
5. Configures TrueNAS UPS service via `midclt`
6. Enables start-on-boot via numeric service ID
7. Registers init script with TrueNAS post-init system
8. Runs init script immediately to bring everything up

---

## Verification

```bash
systemctl status ugreen-ups-driver
upsc ugreen@localhost:3494
journalctl -u ugreen-ups-driver -f
```

Expected example output from `upsc` (OL mode):
```
battery.capacity: 1056
battery.cell.1.voltage: 4.108
battery.cell.2.voltage: 4.108
battery.cell.3.voltage: 4.109
battery.cell.4.voltage: 4.107
battery.charge: 100
battery.charge.low: 20
battery.voltage: 16.432
battery.voltage.nominal: 16
input.current: 2.310
input.voltage: 18.797
output.voltage.nominal: 12
ups.load: 13
ups.status: OL
ups.temperature: 48
...
```

In OB mode, `input.voltage` and `input.current` are absent and `output.voltage` (~12.000) appears in their place.

---

## Pending / Known Issues

1. **battery.runtime accuracy** — bytes `[16-17]` (OB mode) give the BMS estimate, which starts high and rapidly re-settles after mains loss. Could calculate independently from `battery.charge × capacity / load` as a cross-check.
2. **driver.list numeric ID** — `service.update` requires numeric ID which may differ between TrueNAS instances; installer looks this up dynamically but assumes the ID is stable across reboots (appears to be true in practice)
3. **ups.temperature** — byte `[28]` is PLAUSIBLE (42-57°C observed) but not independently verified; single sensor only, identity (ambient/cell/FET) unknown
4. **battery.voltage.nominal** — set to `16` based on 4S Li-ion HID readings (~16.4V max); US3000 is marketed as 24V — discrepancy unresolved
5. **OL CHRG mode (`0x36`)** — byte layout assumed to match OL mode; not directly confirmed from capture data

---