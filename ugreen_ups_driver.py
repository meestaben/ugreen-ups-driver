#!/usr/bin/env python3
"""
ugreen_ups_driver.py — NUT-compatible driver daemon for UGREEN US3000 UPS

Reads HID feature/interrupt reports directly via hidraw and exposes
UPS data over a TCP socket in NUT server protocol format.

Confirmed working with: UGREEN US3000, firmware V3.3, TrueNAS SCALE, NUT 2.8.0.
USB: VID 0x2b89 PID 0xffff

Usage:
    python ugreen_ups_driver.py [--hidraw /dev/hidrawX] [--port 3493] [--debug]

TrueNAS SCALE setup:
    This daemon runs on a separate port (e.g. 3494) and NUT dummy-ups proxies
    it to the standard NUT port (3493). The usbhid kernel driver must be bound
    to the device to create a hidraw node before starting this daemon.

    /etc/nut/ups.conf:
        [ugreen]
            driver = dummy-ups
            port = ugreen@localhost:3494
            desc = "UGREEN US3000"

    /etc/nut/upsmon.conf:
        MONITOR ugreen@localhost:3493 1 upsmon <password> MASTER

Stream report 0x71 byte map — V3.3 firmware:
    NOTE: some of this is a bit speculative, but most of the values look plausible
    The packet layout seems to change depending on operating mode (byte[7]).

    byte[7] mode indicator:
        0x26 = OL          — steady on mains, battery full
        0x36 = OL CHRG     — on mains, battery actively charging
        0x21 = OB          — on battery (mains lost)

    OL mode (0x26) fields:
        [20-21] BE u16 / 100   → battery.voltage     (~30V)
        [24-25] BE u16 / 10    → input.voltage        (~238V UK mains)
        [30]    raw byte %      → ups.load             (~12-13% at NAS idle)
        [32-33] BE u16 / 100   → input.current        (~0.65A at NAS idle)
        [34]    raw byte °C     → ups.temperature      (~46-48°C)
        [36]    raw byte °C     → ups.temperature.2    (~49-50°C)
        [38]    raw byte °C     → ups.temperature.3    (~51-52°C)
        [40]    raw byte °C     → ups.temperature.4    (~48°C)

    OL CHRG mode (0x36) fields:
        [20-21] BE u16 / 100   → battery.voltage     (rising during charge)
        [24-25] BE u16 / 10    → input.voltage        (~238V UK mains)
        [26]    raw byte °C     → ups.temperature      (~34°C)
        [27]    raw byte °C     → ups.temperature.2    (~41°C)
        [28]    raw byte °C     → ups.temperature.3    (~57°C)
        (temperature positions differ from OL mode; only 3 sensors visible)

    OB mode (0x21) fields:
        [22-23] BE u16 seconds  → battery.runtime     (~15800s = ~263min at idle load)
        [35-36] BE u16 mV       → cell group voltage, cell 1
        [37-38] BE u16 mV       → cell group voltage, cell 2
        [39-40] BE u16 mV       → cell group voltage, cell 3
        [41-42] BE u16 mV       → cell group voltage, cell 4
        (4S2P pack: sum of 4 cells × 2 = total pack voltage, ~31.8V full)

Feature report map:
    0x06 [1]      → battery.charge     (0-100%, live SOC from BMS)
    0x09 [1-4] LE → battery.runtime    (seconds; 0xFFFFFFFF = N/A when on mains)
    0x0C          → status block       (NOT used — stream byte[7] is authoritative)
    0x13 [1-2] LE → battery.capacity   (raw value × 4 = Wh; device = 1056Wh)
    0x22, 0x11    → disabled           (return fixed nominal values, not live)
"""

import os
import sys
import fcntl
import struct
import socket
import threading
import time
import argparse
import logging

# HID constants
VENDOR_ID  = 0x2b89
PRODUCT_ID = 0xffff

# IOCTL: HIDIOCGFEATURE(size)
def HIDIOCGFEATURE(size):
    return (0xC0000000 | (size << 16) | (ord('H') << 8) | 0x07)

REPORT_SIZE = 65   # 1 byte report ID + 64 bytes data

# NUT protocol constants
NUT_VERSION   = "2.8.0"
DRIVER_NAME   = "ugreen-hid-py"
DRIVER_VER    = "0.1"

# UPS state (shared between reader thread and server threads)
class UPSState:
    def __init__(self):
        self.lock = threading.Lock()
        self.vars = {
            "device.mfr":              "UGREEN",
            "device.model":            "US3000",
            "device.type":             "ups",
            "driver.name":             DRIVER_NAME,
            "driver.version":          DRIVER_VER,
            "driver.version.internal": DRIVER_VER,
            "ups.mfr":                 "UGREEN",
            "ups.model":               "US3000",
            "ups.status":              "OL",   # safe default — updated by reader
            "battery.charge":          "100",
            "battery.charge.low":      "20",
            "battery.voltage":         "0.00",
            "battery.voltage.nominal": "24",
            "battery.capacity":        "1056", # Wh, from report 0x13 × 4 (updated at runtime)
            "input.voltage":           "0.0",
            "ups.temperature":         "0",
        }
        self.last_update = 0
        self.stale = True


state = UPSState()


# HID reader 

def find_hidraw():
    """Find the hidraw node for VID:2b89 PID:ffff"""
    base = "/sys/class/hidraw"
    if not os.path.exists(base):
        return None
    for node in sorted(os.listdir(base)):
        uevent = os.path.join(base, node, "device", "uevent")
        try:
            with open(uevent) as f:
                content = f.read().upper()
            # Handle both short (2B89:FFFF) and zero-padded (00002B89:0000FFFF) formats
            if ("2B89" in content and "FFFF" in content and
                    ("US3000" in content or "HID_ID" in content)):
                return f"/dev/{node}"
        except OSError:
            continue
    return None


def get_feature_report(fd, report_id):
    buf = bytearray(REPORT_SIZE)
    buf[0] = report_id
    try:
        fcntl.ioctl(fd, HIDIOCGFEATURE(REPORT_SIZE), buf)
        return buf
    except OSError as e:
        logging.debug(f"GET_FEATURE 0x{report_id:02X} failed: {e}")
        return None


def decode_status(fd):
    """Read feature reports and decode into UPS variables."""
    updates = {}

    # Report 0x06: RemainingCapacity ─
    r = get_feature_report(fd, 0x06)
    if r and r[1] <= 100:
        updates["battery.charge"] = str(r[1])

    # Report 0x09: RunTimeToEmpty (32-bit LE, seconds) 
    # 0xFFFFFFFF = N/A (fully charged, not discharging). Don't publish -1;
    # leave battery.runtime absent on mains so the stream decoder can
    # publish a real countdown value when on battery.
    r = get_feature_report(fd, 0x09)
    if r:
        rte = struct.unpack_from('<I', r, 1)[0]
        if rte != 0xFFFFFFFF:
            updates["battery.runtime"] = str(rte)

    # Report 0x0C: AC/status block
    # Status is determined solely from stream byte[7] to avoid oscillation.
    # The feature report status is polled too infrequently and conflicts with
    # the stream's faster updates. Deliberately not reading ups.status here.

    # Report 0x13: DesignCapacity (16-bit LE)
    r = get_feature_report(fd, 0x13)
    if r and len(r) >= 3:
        cap_raw = struct.unpack_from('<H', r, 1)[0]
        if cap_raw > 0:
            updates["battery.capacity"] = str(cap_raw * 4)  # empirical *4 = Wh

    # Report 0x22: Temperature 
    # Disabled: returns a fixed nominal value (20°C), not a live reading.
    # Live temperatures are read from stream report 0x71 (bytes vary by mode).

    # Report 0x11: Output voltage
    # Disabled: returns a fixed nominal value, not a live reading.
    # Output voltage is not currently mapped in the stream report.

    return updates


def read_interrupt(fd, timeout_ms=200):
    """Read one interrupt report from EP1 IN (report ID 0x71)."""
    import select
    rlist, _, _ = select.select([fd], [], [], timeout_ms / 1000.0)
    if not rlist:
        return None
    try:
        data = os.read(fd, 64)
        return data
    except OSError:
        return None


def decode_stream_report(data):
    """Decode the 0x71 interrupt stream report into UPS variables.

    The packet layout seems to change significantly between operating modes.
    byte[7] is the authoritative mode indicator — see module docstring for
    the full byte map per mode. Confirmed by live capture, V3.3 firmware.
    """
    if not data or len(data) < 45:
        return {}
    if data[0] != 0x71:
        return {}

    updates = {}

    # Status: byte[7] seems to be a mode indicator
    # 0x26 = steady OL (fully charged, on mains)
    # 0x36 = charging OL (on mains, battery recharging after discharge)
    # 0x21 = OB (on battery)
    mode = data[7]
    if mode in (0x26, 0x36):
        updates["ups.status"] = "OL CHRG" if mode == 0x36 else "OL"
    elif mode == 0x21:
        updates["ups.status"] = "OB"

    # Fields common to all mains modes (0x26 and 0x36)
    if mode in (0x26, 0x36):

        # battery.voltage: bytes [20-21] BE u16 / 100  (stable across modes)
        batt_v_raw = (data[20] << 8) | data[21]
        if 2000 < batt_v_raw < 4000:
            updates["battery.voltage"] = f"{batt_v_raw / 100.0:.2f}"

        # input.voltage: bytes [24-25] BE u16 / 10  (stable across modes)
        in_v_raw = (data[24] << 8) | data[25]
        if 1500 < in_v_raw < 3000:
            updates["input.voltage"] = f"{in_v_raw / 10.0:.1f}"

    if mode == 0x26:
        # Steady OL mode only

        # input.current: bytes [32-33] BE u16 / 100  (~0.65A at idle)
        in_i_raw = (data[32] << 8) | data[33]
        if 0 < in_i_raw < 5000:
            updates["input.current"] = f"{in_i_raw / 100.0:.2f}"

        # ups.load: byte [30] raw %  (~12-13% at idle = 120-130W)
        load = data[30]
        if 0 <= load <= 100:
            updates["ups.load"] = str(load)

        # ups.temperature: bytes [34],[36],[38],[40] raw °C
        # Interleaved with 0x0F/0x10 separator bytes at [35],[37],[39],[41]
        for idx, key in [(34, "ups.temperature"),
                         (36, "ups.temperature.2"),
                         (38, "ups.temperature.3"),
                         (40, "ups.temperature.4")]:
            temp = data[idx]
            if 0 < temp < 100:
                updates[key] = str(temp)

    elif mode == 0x36:
        # Charging OL mode — different layout
        # Temperatures shift to bytes [26],[27],[28]; only 3 sensors in this mode
        for idx, key in [(26, "ups.temperature"),
                         (27, "ups.temperature.2"),
                         (28, "ups.temperature.3")]:
            temp = data[idx]
            if 0 < temp < 100:
                updates[key] = str(temp)
        # Clear fields that don't apply in charging mode
        updates["ups.temperature.4"] = None
        updates["ups.load"] = None
        updates["input.current"] = None
        updates["battery.runtime"] = None

    elif mode == 0x21:
        # ON BATTERY mode

        # ups.load: byte [31] raw % (shifts by 1 vs byte[30] in OL mode)
        load = data[31]
        if 0 <= load <= 100:
            updates["ups.load"] = str(load)

        # ups.temperature: bytes [26],[27],[28] raw °C — same positions as CHRG mode
        # Observed 38°C, 40°C, 43°C at idle on battery
        for idx, key in [(26, "ups.temperature"),
                         (27, "ups.temperature.2"),
                         (28, "ups.temperature.3")]:
            temp = data[idx]
            if 0 < temp < 100:
                updates[key] = str(temp)
        updates["ups.temperature.4"] = None  # only 3 sensors visible in this mode

        # battery.runtime: bytes [22-23] BE u16 in seconds (~16000s = 267min)
        runtime_raw = (data[22] << 8) | data[23]
        if runtime_raw > 0:
            updates["battery.runtime"] = str(runtime_raw)

        # battery cell voltages: bytes [35-36],[37-38],[39-40],[41-42] BE/1000
        # 4S2P Li-ion pack? — BMS reports one group of 4 cells (~3.97V each).
        # Full pack voltage = cell group sum × 2 (matches ~30V seen on mains).
        cell_sum = 0
        valid = True
        for idx in [35, 37, 39, 41]:
            cell_mv = (data[idx] << 8) | data[idx + 1]
            if not (3000 < cell_mv < 4500):
                valid = False
                break
            cell_sum += cell_mv
        if valid:
            updates["battery.voltage"] = f"{cell_sum * 2 / 1000.0:.2f}"

        # Clear input voltage — no mains present
        updates["input.voltage"] = "0.0"

    return updates


def hid_reader_loop(hidraw_path, poll_interval=2.0):
    """Main HID polling loop — runs in a background thread."""
    global state

    logging.info(f"HID reader starting on {hidraw_path}")

    while True:
        try:
            fd = os.open(hidraw_path, os.O_RDWR | os.O_NONBLOCK)
            logging.info(f"Opened {hidraw_path}")

            feature_countdown = 0
            # Status debounce: require 3 consecutive identical readings
            # before publishing a status change, to avoid oscillation alerts
            status_candidate = None
            status_count = 0
            STATUS_DEBOUNCE = 3

            while True:
                # Read interrupt report (fast path, ~1s cadence from device)
                stream_data = read_interrupt(fd, timeout_ms=1500)
                updates = {}

                if stream_data:
                    updates.update(decode_stream_report(stream_data))

                # Debounce ups.status changes
                if "ups.status" in updates:
                    new_status = updates["ups.status"]
                    with state.lock:
                        current_status = state.vars.get("ups.status", "OL")
                    if new_status == current_status:
                        status_candidate = None
                        status_count = 0
                    elif new_status == status_candidate:
                        status_count += 1
                        if status_count < STATUS_DEBOUNCE:
                            del updates["ups.status"]
                            logging.debug(f"Status debounce: {new_status} ({status_count}/{STATUS_DEBOUNCE})")
                    else:
                        status_candidate = new_status
                        status_count = 1
                        del updates["ups.status"]
                        logging.debug(f"Status debounce: new candidate {new_status} (1/{STATUS_DEBOUNCE})")

                # Poll feature reports every ~10 seconds
                feature_countdown -= poll_interval
                if feature_countdown <= 0:
                    feature_updates = decode_status(fd)
                    updates.update(feature_updates)
                    feature_countdown = 10.0

                if updates:
                    with state.lock:
                        for k, v in updates.items():
                            if v is None or v == "":
                                state.vars.pop(k, None)
                            else:
                                state.vars[k] = v
                        state.last_update = time.time()
                        state.stale = False
                    logging.debug(f"Updated: {updates}")

                time.sleep(poll_interval)

        except OSError as e:
            logging.warning(f"HID error: {e} — retrying in 5s")
            with state.lock:
                state.stale = True
        except Exception as e:
            logging.error(f"Unexpected error in HID reader: {e}")
            with state.lock:
                state.stale = True

        time.sleep(5)


# A minimal viable NUT protocol server

UPS_NAME = "ugreen"

def handle_client(conn, addr):
    """Handle a single NUT client connection."""
    logging.debug(f"Client connected: {addr}")
    try:
        conn.settimeout(30)
        buf = ""
        while True:
            try:
                chunk = conn.recv(1024).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        response = process_command(line)
                        if response:
                            conn.sendall((response + "\n").encode())
                        if line.upper() == "LOGOUT":
                            return
            except socket.timeout:
                break
    except Exception as e:
        logging.debug(f"Client {addr} error: {e}")
    finally:
        conn.close()
        logging.debug(f"Client disconnected: {addr}")


def process_command(cmd):
    """Process a NUT protocol command and return response string."""
    parts = cmd.split()
    if not parts:
        return "ERR UNKNOWN-COMMAND"

    verb = parts[0].upper()

    # GET VER
    if verb == "VER":
        return f"Network UPS Tools upsd {NUT_VERSION}"

    # NETVER
    if verb == "NETVER":
        return "NET VER 1.1"

    # LIST UPS
    if verb == "LIST" and len(parts) >= 2 and parts[1].upper() == "UPS":
        return f"BEGIN LIST UPS\nUPS {UPS_NAME} \"UGREEN US3000\"\nEND LIST UPS"

    # LIST VAR <upsname>
    if verb == "LIST" and len(parts) >= 3 and parts[1].upper() == "VAR":
        name = parts[2]
        if name != UPS_NAME:
            return "ERR UNKNOWN-UPS"
        with state.lock:
            lines = [f"VAR {name} {k} \"{v}\"" for k, v in sorted(state.vars.items())]
        return f"BEGIN LIST VAR {name}\n" + "\n".join(lines) + f"\nEND LIST VAR {name}"

    # LIST CMD <upsname>
    if verb == "LIST" and len(parts) >= 3 and parts[1].upper() == "CMD":
        name = parts[2]
        if name != UPS_NAME:
            return "ERR UNKNOWN-UPS"
        return (f"BEGIN LIST CMD {name}\n"
                f"CMD {name} beeper.toggle\n"
                f"END LIST CMD {name}")

    # GET VAR <upsname> <varname>
    if verb == "GET" and len(parts) >= 4 and parts[1].upper() == "VAR":
        name = parts[2]
        varname = parts[3]
        if name != UPS_NAME:
            return "ERR UNKNOWN-UPS"
        with state.lock:
            val = state.vars.get(varname)
        if val is None:
            return "ERR VAR-NOT-SUPPORTED"
        return f"VAR {name} {varname} \"{val}\""

    # GET UPSDESC <upsname>
    if verb == "GET" and len(parts) >= 3 and parts[1].upper() == "UPSDESC":
        name = parts[2]
        if name != UPS_NAME:
            return "ERR UNKNOWN-UPS"
        return f"UPSDESC {name} \"UGREEN US3000 UPS\""

    # GET NUMLOGINS <upsname>
    if verb == "GET" and len(parts) >= 3 and parts[1].upper() == "NUMLOGINS":
        return f"NUMLOGINS {parts[2]} 0"

    # USERNAME / PASSWORD (accept anything — no auth needed for read-only)
    if verb in ("USERNAME", "PASSWORD"):
        return "OK"

    # LOGIN <upsname>
    if verb == "LOGIN":
        return "OK"

    # LOGOUT
    if verb == "LOGOUT":
        return "OK Goodbye"

    # STARTTLS — not supported
    if verb == "STARTTLS":
        return "ERR FEATURE-NOT-CONFIGURED"

    return "ERR UNKNOWN-COMMAND"


def run_server(host="0.0.0.0", port=3493):
    """Run the NUT-compatible TCP server."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(10)
    logging.info(f"NUT server listening on {host}:{port}")

    while True:
        try:
            conn, addr = srv.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
        except Exception as e:
            logging.error(f"Server error: {e}")


# Entrypoint

def main():
    parser = argparse.ArgumentParser(description="UGREEN US3000 NUT driver daemon")
    parser.add_argument("--hidraw", default=None,
                        help="hidraw device path (auto-detected if omitted)")
    parser.add_argument("--port", type=int, default=3493,
                        help="TCP port for NUT protocol (default: 3493)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--poll", type=float, default=1.0,
                        help="Poll interval in seconds (default: 2.0)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    # Find hidraw device
    hidraw = args.hidraw or find_hidraw()
    if not hidraw:
        logging.error("Could not find UGREEN US3000 hidraw device.")
        logging.error("Make sure usbhid kernel driver is bound:")
        logging.error("  echo '3-6:1.0' > /sys/bus/usb/drivers/usbhid/bind")
        sys.exit(1)

    logging.info(f"Using hidraw device: {hidraw}")

    # Start HID reader thread
    reader = threading.Thread(
        target=hid_reader_loop,
        args=(hidraw, args.poll),
        daemon=True,
        name="hid-reader"
    )
    reader.start()

    # Run NUT server (blocks)
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
