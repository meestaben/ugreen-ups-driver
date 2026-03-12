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

#### OL mode (`0x26`)
| Bytes | Decode | NUT Variable |
|-------|--------|--------------|
| `[20-21]` | BE u16 ÷ 100 | `battery.voltage` (~30V) |
| `[24-25]` | BE u16 ÷ 10 | `input.voltage` (~238V UK mains) |
| `[30]` | raw byte % | `ups.load` (~12-13% idle) |
| `[32-33]` | BE u16 ÷ 100 | `input.current` (~0.65A idle) |
| `[34],[36],[38],[40]` | raw byte °C | `ups.temperature` ×4 (~46-52°C) |

#### OL CHRG mode (`0x36`)
| Bytes | Decode | NUT Variable |
|-------|--------|--------------|
| `[20-21]` | BE u16 ÷ 100 | `battery.voltage` (rising) |
| `[24-25]` | BE u16 ÷ 10 | `input.voltage` |
| `[26],[27],[28]` | raw byte °C | `ups.temperature` ×3 (~34, 41, 57°C) |

#### OB mode (`0x21`)
| Bytes | Decode | NUT Variable |
|-------|--------|--------------|
| `[22-23]` | BE u16 seconds | `battery.runtime` (~14200-15800s) |
| `[26],[27],[28]` | raw byte °C | `ups.temperature` ×3 (~38-39, 41, 43-58°C) |
| `[31]` | raw byte % | `ups.load` (~14-20%) |
| `[35-36],[37-38],[39-40],[41-42]` | BE u16 mV each | cell voltages (4S2P pack, sum×2 = pack voltage) |
| `[43]` | raw byte % | `battery.charge` (live SOC when discharging) |

### Feature Reports (confirmed)
| Report | Bytes | NUT Variable | Notes |
|--------|-------|--------------|-------|
| `0x06` | `[1]` 0-100% | `battery.charge` | Live SOC from BMS |
| `0x09` | `[1-4]` LE | `battery.runtime` | Returns `0xFFFFFFFF` always — not used |
| `0x13` | `[1-2]` LE × 4 = Wh | `battery.capacity` | 264 × 4 = **1056 Wh** |

### Notes
- `battery.charge` byte `[43]` is fixed at 94 when fully charged; becomes live SOC below ~90% on discharge
- 4S2P Li-ion pack: 4 cell voltages reported (~3.97V each at full), sum × 2 = pack voltage
- `ups.temperature.3` (OB mode) shows transient spikes to 50-58°C — single-packet firmware artefacts, ignore
- `ups.temperature.4` is None in OB mode (not reported)
- `ups.status` uses **asymmetric debounce**: OL→OB requires 3 consecutive readings (~3s); OB→OL requires 10 (~10s) to prevent flicker on mains restore

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
upsc ugreen@localhost:3493   # via TrueNAS NUT relay
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
upsc ugreen@localhost:3493
journalctl -u ugreen-ups-driver -f
```

Expected example output from `upsc`:
```
battery.charge: 94
battery.runtime: 15200
battery.voltage: 30.49
input.voltage: 238.0
ups.load: 13
ups.status: OL
ups.temperature: 46
...
```

---

## Pending / Known Issues

1. **battery.runtime accuracy** — bytes `[22-23]` is the BMS estimate. Could calculate independently from `battery.charge × capacity / load`
2. **driver.list numeric ID** — `service.update` requires numeric ID which may differ between TrueNAS instances; installer looks this up dynamically but assumes the ID is stable across reboots (appears to be true in practice)

As a general point: this is all *extremely* alpha and based off an afternoon of hacking about with the device's usb data, so expect it to break or provide inaccurate information! I'm not even convinced the design is right, but it does the job I need it to do which is effect an orderly shutdown on power loss and low UPS battery. 

---