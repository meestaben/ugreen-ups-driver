# UGREEN US3000 UPS - Custom NUT Driver

Pure Python NUT driver for the UGREEN US3000 UPS on TrueNAS SCALE (tested on DXP4800 Plus). The built-in `usbhid-ups` driver only reports on/battery status for this device; this driver exposes battery voltage, cell voltages, charge %, runtime, load, and input/output voltages.

The US3000 uses vendor-defined HID page `0xFF00`, report `0x71`, rather than standard HID Power Device pages. The driver reads `/dev/hidrawN` directly via `HIDIOCGFEATURE` ioctl and interrupt stream, decodes the proprietary byte map, and serves NUT protocol on a secondary port (default 3494). TrueNAS NUT is configured with `dummy-ups` pointing at `localhost:3494`.

USB: VID `2b89` PID `ffff`. Tested with firmware **V3.3** only.

## Repository Contents

| File | Description |
|------|-------------|
| `ugreen_ups_driver.py` | Python NUT driver - reads HID raw device, serves NUT protocol |
| `install-ugreen-ups.sh` | Installer/uninstaller - idempotent, configures everything via TrueNAS middleware API |

The installer generates these files at install time (not committed - contain templated paths):
- `find-and-bind-ups.sh` - locates USB device by VID/PID and binds hidraw
- `ugreen-ups-driver.service` - systemd service unit
- `ugreen-ups-init.sh` - TrueNAS post-init script (re-registers service and patches driver list after updates)

---

## USB / HID Architecture

TrueNAS NUT normally claims the device via `usbfs` (libusb). To use `hidraw` instead:

1. Stop `nut-driver@ugreen` (releases the usbfs claim)
2. Bind interface `{USB_DEV}:1.0` to `usbhid` kernel driver
3. `/dev/hidrawN` appears - driver opens it directly

`find-and-bind-ups.sh` handles this at boot, scanning sysfs by VID/PID.

---

## HID Report Byte Map (V3.3 firmware)

### Stream Report `0x71`

**`byte[7]` appears to be the authoritative status byte:**

| `byte[7]` | Mode | NUT `ups.status` |
|-----------|------|-----------------|
| `0x26` | On mains, battery full | `OL` |
| `0x36` | On mains, battery charging | `OL CHRG` |
| `0x21` | On battery (mains lost) | `OB` |

**Device note:** The US3000 is a DC-only UPS - input is 12V/19V/20V DC from a power brick; output is 12V DC to the NAS.

Confidence key: **PROBABLE** = verified from OL<->OB transition capture; **PLAUSIBLE** = value in expected range but not cross-verified; **ASSUMED** = extrapolated from another mode, not directly captured.

#### All modes - fields present regardless of mode
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[22-23]` | BE u16 ÷ 1000 V | `battery.voltage` (~16.4V full, 4S Li-ion) | PROBABLE |
| `[28]` | unknown | not published - oscillates ~23<->55 on ~15 min cycle; likely charger state, not temperature | UNKNOWN |
| `[35-36]` | BE u16 ÷ 1000 V | `battery.cell.1.voltage` (~4.108V full) | PROBABLE |
| `[37-38]` | BE u16 ÷ 1000 V | `battery.cell.2.voltage` | PROBABLE |
| `[39-40]` | BE u16 ÷ 1000 V | `battery.cell.3.voltage` | PROBABLE |
| `[41-42]` | BE u16 ÷ 1000 V | `battery.cell.4.voltage` | PROBABLE |
| `[43]` | raw byte % | `battery.charge` | PROBABLE |
| `[45]` | unknown | not published - raw ~29-34; suspected ambient sensor but no variation confirmed | UNKNOWN |
| `[46]` | unknown | not published - raw constant ~49; no variation observed | UNKNOWN |

Cell voltages sum to ≈ `battery.voltage` (4S pack, confirmed by teardown[1]: 4× SunPower INR18650-3000 NMC in series). `battery.voltage.nominal` = `14` (3.6V/cell × 4). `battery.capacity` = **43 Wh** (3000 mAh × 14.4V). The UGREEN "12000 mAh" figure is the sum of all four individual cell capacities.

**`[18-19]`** measures different physical nodes per mode: OL = power brick input, OB = regulated 12V output to NAS. Published as different NUT variables accordingly.

#### OL mode (`0x26`) additional fields
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[16-17]` | BE u16 ÷ 1000 V | not published - DC input voltage ~18.89V, second measurement point ~90-100mV above `[18-19]` | PROBABLE |
| `[18-19]` | BE u16 ÷ 1000 V | `input.voltage` (~18.8V DC from 19V power brick under load) | PROBABLE |
| `[24-25]` | BE u16 ÷ 1000 A | `input.current` (~2.3A; 19V × 2.3A ≈ 43W ≈ NAS load) | PLAUSIBLE |
| `[30]` | raw byte % | `ups.load` (~11-15% idle) | PLAUSIBLE |

#### OL CHRG mode (`0x36`) additional fields
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[16-17]` | BE u16 ÷ 1000 V | not published - DC input voltage ~18.87V (same meaning as OL; previously misidentified as `battery.runtime`) | PROBABLE |
| `[18-19]` | BE u16 ÷ 1000 V | `input.voltage` | ASSUMED |
| `[24-25]` | BE u16 ÷ 1000 A | `input.current` | ASSUMED |
| `[29-30]` | BE u16 mA | not published - charge current (~710 mA active charge; `[29-30]` as pair) | PLAUSIBLE |
| `[30]` | raw byte % | `ups.load` (when `[29]=0`; range check 0-100 rejects the OL_CHRG value of ~196 in `[30]`) | PROBABLE (OL); UNKNOWN (OL_CHRG) |

#### OB mode (`0x21`) additional fields
| Bytes | Decode | NUT Variable | Confidence |
|-------|--------|--------------|------------|
| `[16-17]` | BE u16 seconds | `battery.runtime` (BMS estimate; starts high ~11000s then rapidly converges; settled value observed ~383-390s after 12 min discharge) | PROBABLE |
| `[18-19]` | BE u16 ÷ 1000 V | `output.voltage` (~12.0V DC regulated output to NAS) | PROBABLE |
| `[24-25]` | BE u16 ÷ 1000 A | `battery.current` (discharge, ~3.5-3.7A) | PLAUSIBLE |
| `[31]` | raw byte % | `ups.load` (~14% at idle NAS; field shifts from `[30]` in other modes) | PROBABLE |

### Feature Reports
| Report | Bytes | NUT Variable | Notes |
|--------|-------|--------------|-------|
| `0x01` | - | `ups.alarm` | PresentStatus register; bit 7 = NeedReplacement → published as `"REPLACE BATTERY"` when set |
| `0x06` | `[1]` 0-100% | `battery.charge` | BMS SOC - fallback/cross-check alongside stream `[43]` |
| `0x06` | `[2-5]` LE u32 | `battery.runtime` | RunTimeToEmpty in seconds; `0xFFFFFFFF` = N/A (on mains); may lag stream `[16-17]` after mains loss |
| `0x09` | - | not used | DelayBeforeShutdown - returns `0xFFFFFFFF` when no shutdown scheduled; previously misidentified as RunTimeToEmpty |
| `0x13` | `[1-2]` LE u16 | not used | DesignCapacity - encoding unknown (raw × 4 ≈ 1056 Wh, ~24× actual); `battery.capacity` hardcoded to **43 Wh** from teardown |
| `0x22` | - | not used | Vendor-specific FF.004d; value 20 = RemainingCapacityLimit threshold |

### Notes
- `ups.status` debounce: 3 consecutive matching reads (~3s) required before publishing a change
- `battery.cell.*.voltage` are non-standard NUT variables; published for cell balance monitoring

---

## Driver Design

- Pure Python, no external dependencies beyond stdlib
- hidraw via `HIDIOCGFEATURE` ioctl + `select()`/`read()` for interrupt stream
- NUT protocol on configurable port (default `3494`)
- Auto-detects hidraw by scanning `/sys/class/hidraw/*/device/uevent` for VID `2B89`
- Stream polled every 1s, feature reports every 10s
- `None` value removes stale fields when mode changes (e.g. `input.voltage` absent in OB)
- Status transitions debounced: 3 consecutive matching reads before publishing

**Usage:**
```bash
python3 ugreen_ups_driver.py --port 3494
upsc ugreen@localhost:3494   # via TrueNAS NUT relay
```

---

## TrueNAS Integration

### NUT configuration (managed by TrueNAS - do not edit manually)

`/etc/nut/ups.conf`:
```
[ugreen]
    driver = dummy-ups
    port = ugreen@localhost:3494
    desc = "UGREEN US3000"
```

`/etc/nut/nut.conf`: `MODE=netserver`

### driver.list entry (wiped by TrueNAS updates - init script re-patches on boot)
```
"UGREEN"	"ups"	"1"	"US3000"	""	"dummy-ups"
```

### Middleware API reference
```bash
# Configure UPS (install)
midclt call ups.update '{"driver": "dummy-ups$US3000", "port": "ugreen@localhost:3494", ...}'

# Revert UPS config to factory defaults (uninstall)
# Must use SLAVE mode - driver field is required when mode=MASTER, causing validation failure
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
battery.capacity: 43
battery.cell.1.voltage: 4.108
battery.cell.2.voltage: 4.108
battery.cell.3.voltage: 4.109
battery.cell.4.voltage: 4.107
battery.charge: 100
battery.charge.low: 20
battery.voltage: 16.432
battery.voltage.nominal: 14
input.current: 2.310
input.voltage: 18.797
output.voltage.nominal: 12
ups.load: 13
ups.status: OL
```

In OB mode, `input.voltage` and `input.current` are absent and `output.voltage` (~12.000) appears in their place.

---

## Pending / Known Issues

1. **battery.runtime accuracy** - bytes `[16-17]` (OB mode) give the BMS estimate, which starts very high and rapidly converges after mains loss. Could calculate independently from `battery.charge × capacity / load` as a cross-check.
2. **driver.list numeric ID** - `service.update` requires numeric ID which may differ between TrueNAS instances; installer looks this up dynamically but assumes the ID is stable across reboots (appears to be true in practice)
3. **battery.voltage.nominal** - set to `14` (3.6V/cell × 4 per teardown); actual full charge reads ~16.4V, so this is conservative
4. **[20-21] unknown** - near-zero in OB (~0.04A), ~3.0A in OL/OL_CHRG; likely a second current measurement but relationship to `[24-25]` unclear; not currently published

---

## Disclaimer

This software is provided as-is for experimental use. The author accepts no responsibility for any damage to hardware, data loss, or system instability resulting from its use. Use at your own risk!

---

[1] Credit: https://www.chargerlab.com/teardown-of-ugreen-120w-dc-ups-us3000/