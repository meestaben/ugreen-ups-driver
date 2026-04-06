#!/bin/bash
# UGREEN US3000 UPS NUT Driver Installer/Uninstaller
# Usage: ./install-ugreen-ups.sh [install|uninstall] [install_path]
# Default action: install
# Default install path: /mnt/tank/system/ugreen-ups
# Safe to re-run (idempotent)

set -e

###############################################################################
# CONFIG
###############################################################################
ACTION="${1:-install}"
INSTALL_PATH="${2:-/mnt/tank/system/ugreen-ups}"
DRIVER_SCRIPT="ugreen_ups_driver.py"
BIND_SCRIPT="find-and-bind-ups.sh"
SERVICE_NAME="ugreen-ups-driver"
NUT_DRIVER_LIST="/usr/share/nut/driver.list"
NUT_DRIVER_ENTRY='"UGREEN"\t"ups"\t"1"\t"US3000"\t""\t"dummy-ups"'
NUT_DRIVER_GREP='UGREEN.*US3000.*dummy-ups'
UPS_IDENTIFIER="ugreen"
UPS_PORT="3494"
UPS_DESC="UGREEN US3000"
PASSWORD_FILE="$INSTALL_PATH/.upsmon-password"
UDEV_RULE="/etc/udev/rules.d/99-ugreen-ups.rules"

###############################################################################
# HELPERS
###############################################################################
info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
error() { echo "[ERROR] $*" >&2; exit 1; }

check_root() {
    [ "$(id -u)" = "0" ] || error "Must be run as root"
}

check_install_path() {
    case "$INSTALL_PATH" in
        /mnt/*) ;;
        *) error "Install path must be under /mnt (got: $INSTALL_PATH)" ;;
    esac
    mountpoint -q "$(echo "$INSTALL_PATH" | cut -d'/' -f1-3)" \
        || error "Install path does not appear to be on a mounted pool: $INSTALL_PATH"
    mkdir -p "$INSTALL_PATH"
    info "Install path: $INSTALL_PATH"
}

generate_password() {
    tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32
}

find_usb_dev() {
    for vendorfile in /sys/bus/usb/devices/*/idVendor; do
        if [ "$(cat $vendorfile 2>/dev/null)" = "2b89" ]; then
            devpath=$(dirname $vendorfile)
            if [ "$(cat $devpath/idProduct 2>/dev/null)" = "ffff" ]; then
                basename $devpath
                return 0
            fi
        fi
    done
    return 1
}

get_ups_service_id() {
    midclt call service.query '[]' 2>/dev/null \
        | python3 -c "
import sys, json
services = json.load(sys.stdin)
for s in services:
    if s.get('service') == 'ups':
        print(s['id'])
        break
" 2>/dev/null || true
}

confirm_proceed() {
    echo "$1"
    printf "Continue? [y/N] "
    read -r reply
    case "$reply" in
        [yY]) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
}

###############################################################################
# UNINSTALL
###############################################################################
do_uninstall() {
    confirm_proceed "This will stop the UGREEN UPS driver, revert the TrueNAS UPS service configuration to factory defaults, and remove all installed files from $INSTALL_PATH."
    info "Uninstalling UGREEN UPS driver"

    # Stop and disable our systemd service
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Stopping $SERVICE_NAME"
        systemctl stop "$SERVICE_NAME" || true
    fi
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Disabling $SERVICE_NAME"
        systemctl disable "$SERVICE_NAME" || true
    fi
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload

    # Remove udev rule
    if [ -f "$UDEV_RULE" ]; then
        info "Removing udev rule"
        rm -f "$UDEV_RULE"
        udevadm control --reload-rules
    fi

    # Unbind from usbhid
    info "Unbinding USB device from usbhid"
    USB_DEV=$(find_usb_dev || true)
    if [ -n "$USB_DEV" ]; then
        echo "${USB_DEV}:1.0" > /sys/bus/usb/drivers/usbhid/unbind 2>/dev/null || true
        info "USB device unbound from usbhid"
    else
        warn "USB device not found — skipping unbind"
    fi

    # Remove dummy-ups entry from driver list
    if grep -q "$NUT_DRIVER_GREP" "$NUT_DRIVER_LIST" 2>/dev/null; then
        info "Removing dummy-ups entry from $NUT_DRIVER_LIST"
        grep -v "$NUT_DRIVER_GREP" "$NUT_DRIVER_LIST" > "${NUT_DRIVER_LIST}.tmp"
        mv "${NUT_DRIVER_LIST}.tmp" "$NUT_DRIVER_LIST"
    fi

    # Revert TrueNAS UPS config to factory defaults (SLAVE mode — driver is optional in this mode)
    info "Reverting TrueNAS UPS config to factory defaults"
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
    }' > /dev/null && info "TrueNAS UPS config reverted to factory defaults" \
       || warn "Could not revert UPS config via API — revert manually in TrueNAS UI"

    # Stop and disable TrueNAS UPS service
    info "Stopping and disabling TrueNAS UPS service"
    midclt call service.stop '"ups"' '{}' > /dev/null 2>&1 \
        && info "TrueNAS UPS service stopped" \
        || warn "Could not stop UPS service via API"

    UPS_SERVICE_ID=$(get_ups_service_id)
    if [ -n "$UPS_SERVICE_ID" ]; then
        midclt call service.update "$UPS_SERVICE_ID" '{"enable": false}' > /dev/null 2>&1 \
            && info "TrueNAS UPS service disabled (id=$UPS_SERVICE_ID)" \
            || warn "Could not disable UPS service — disable manually in TrueNAS UI → Services"
    else
        warn "Could not determine UPS service ID — disable start on boot manually in TrueNAS UI → Services"
    fi

    # Remove init script registration from TrueNAS
    info "Removing init script registration from TrueNAS"
    EXISTING_ID=$(midclt call initshutdownscript.query \
        "[[\"script\", \"=\", \"${INSTALL_PATH}/ugreen-ups-init.sh\"]]" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')" \
        2>/dev/null || true)
    if [ -n "$EXISTING_ID" ]; then
        midclt call initshutdownscript.delete "$EXISTING_ID" > /dev/null \
            && info "Init script unregistered (id=$EXISTING_ID)" \
            || warn "Could not unregister init script — remove manually in TrueNAS UI"
    else
        info "Init script not registered, nothing to remove"
    fi

    # Remove install directory
    if [ -d "$INSTALL_PATH" ]; then
        info "Removing install directory $INSTALL_PATH"
        rm -rf "$INSTALL_PATH"
    fi

    echo ""
    echo "================================================================"
    echo " UGREEN UPS driver uninstalled"
    echo " TrueNAS UPS service stopped and reverted to usbhid-ups (OEM)"
    echo " Reboot recommended to fully restore OEM state"
    echo "================================================================"
}

###############################################################################
# INSTALL
###############################################################################
do_install() {
    confirm_proceed "This will install the UGREEN UPS driver to $INSTALL_PATH and overwrite any existing TrueNAS UPS service configuration."
    check_install_path

    # Check driver script is present in source directory
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    [ -f "$SCRIPT_DIR/$DRIVER_SCRIPT" ] \
        || error "Cannot find $DRIVER_SCRIPT in $SCRIPT_DIR — run installer from the source directory"

    #--------------------------------------------------------------------------
    # STEP 1: Copy driver files
    #--------------------------------------------------------------------------
    info "Copying driver files to $INSTALL_PATH"
    cp "$SCRIPT_DIR/$DRIVER_SCRIPT" "$INSTALL_PATH/"
    chmod +x "$INSTALL_PATH/$DRIVER_SCRIPT"

    #--------------------------------------------------------------------------
    # STEP 2: Write find-and-bind script
    #--------------------------------------------------------------------------
    info "Writing $BIND_SCRIPT"
    cat > "$INSTALL_PATH/$BIND_SCRIPT" << 'BIND_EOF'
#!/bin/bash
# Find UGREEN US3000 USB interface and ensure hidraw is bound
VID="2b89"
PID="ffff"

USB_DEV=""
for vendorfile in /sys/bus/usb/devices/*/idVendor; do
    if [ "$(cat $vendorfile 2>/dev/null)" = "$VID" ]; then
        devpath=$(dirname $vendorfile)
        if [ "$(cat $devpath/idProduct 2>/dev/null)" = "$PID" ]; then
            USB_DEV=$(basename $devpath)
            break
        fi
    fi
done

if [ -z "$USB_DEV" ]; then
    echo "ERROR: UGREEN US3000 not found (VID $VID PID $PID)" >&2
    exit 1
fi

IFACE="${USB_DEV}:1.0"
echo "Found device at $IFACE"

find_hidraw() {
    for hidraw in /sys/class/hidraw/*/device; do
        [ -e "$hidraw" ] || continue
        if readlink -f "$hidraw" | grep -q "$USB_DEV"; then
            basename $(dirname $hidraw)
            return 0
        fi
    done
    return 1
}

if find_hidraw; then
    echo "hidraw already present: $(find_hidraw)"
    exit 0
fi

echo "$IFACE" > /sys/bus/usb/drivers/usbfs/unbind 2>/dev/null
echo "$IFACE" > /sys/bus/usb/drivers/usbhid/bind 2>/dev/null

for i in $(seq 1 10); do
    result=$(find_hidraw)
    if [ $? -eq 0 ]; then
        echo "hidraw bound: $result"
        exit 0
    fi
    sleep 1
done

echo "ERROR: hidraw did not appear after binding" >&2
exit 1
BIND_EOF
    chmod +x "$INSTALL_PATH/$BIND_SCRIPT"

    #--------------------------------------------------------------------------
    # STEP 3: Write systemd service file (templated with install path)
    #--------------------------------------------------------------------------
    info "Writing systemd service file"
    cat > "$INSTALL_PATH/$SERVICE_NAME.service" << SERVICE_EOF
[Unit]
Description=UGREEN US3000 UPS NUT Driver
Before=nut-driver@${UPS_IDENTIFIER}.service nut-server.service
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStartPre=/bin/systemctl --no-block stop nut-monitor nut-server nut-driver@${UPS_IDENTIFIER}
ExecStartPre=/bin/sleep 2
ExecStartPre=${INSTALL_PATH}/${BIND_SCRIPT}
ExecStart=/usr/bin/python3 ${INSTALL_PATH}/${DRIVER_SCRIPT} --port ${UPS_PORT}
ExecStartPost=/bin/bash -c "sleep 3 && systemctl --no-block start nut-driver@${UPS_IDENTIFIER} nut-server nut-monitor"
Restart=on-failure
RestartSec=10


[Install]
WantedBy=multi-user.target
SERVICE_EOF

    #--------------------------------------------------------------------------
    # STEP 4: Write init script (templated, registered with TrueNAS)
    #--------------------------------------------------------------------------
    info "Writing init script"
    cat > "$INSTALL_PATH/ugreen-ups-init.sh" << INIT_EOF
#!/bin/bash
# UGREEN UPS boot init script
# Registered with TrueNAS as a Post Init script
# Do not edit directly — regenerated by install-ugreen-ups.sh

INSTALL_PATH="${INSTALL_PATH}"
SERVICE_NAME="${SERVICE_NAME}"
NUT_DRIVER_LIST="${NUT_DRIVER_LIST}"
NUT_DRIVER_GREP="${NUT_DRIVER_GREP}"
NUT_DRIVER_ENTRY="${NUT_DRIVER_ENTRY}"

# Patch dummy-ups into NUT driver list if missing (wiped by TrueNAS updates)
if ! grep -q "\$NUT_DRIVER_GREP" "\$NUT_DRIVER_LIST" 2>/dev/null; then
    printf "\$NUT_DRIVER_ENTRY\n" >> "\$NUT_DRIVER_LIST"
    logger -t ugreen-ups "Patched dummy-ups entry into \$NUT_DRIVER_LIST"
fi

# Install udev rule for USB permissions
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2b89", ATTR{idProduct}=="ffff", MODE="0660", GROUP="nut"' \
    > /etc/udev/rules.d/99-ugreen-ups.rules
udevadm control --reload-rules && udevadm trigger

# Install and enable systemd service from pool copy
cp "\${INSTALL_PATH}/\${SERVICE_NAME}.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "\$SERVICE_NAME"
systemctl start "\$SERVICE_NAME"

logger -t ugreen-ups "UGREEN UPS driver init complete"
INIT_EOF
    chmod +x "$INSTALL_PATH/ugreen-ups-init.sh"

    #--------------------------------------------------------------------------
    # STEP 5: Patch NUT driver list
    #--------------------------------------------------------------------------
    info "Patching NUT driver list"
    if grep -q "$NUT_DRIVER_GREP" "$NUT_DRIVER_LIST" 2>/dev/null; then
        info "dummy-ups entry already present in $NUT_DRIVER_LIST"
    else
        printf "$NUT_DRIVER_ENTRY\n" >> "$NUT_DRIVER_LIST"
        info "Added dummy-ups entry to $NUT_DRIVER_LIST"
    fi

    #--------------------------------------------------------------------------
    # STEP 6: Generate or reuse upsmon password
    #--------------------------------------------------------------------------
    if [ -f "$PASSWORD_FILE" ]; then
        UPSMON_PASSWORD=$(cat "$PASSWORD_FILE")
        info "Using existing upsmon password"
    else
        UPSMON_PASSWORD=$(generate_password)
        echo "$UPSMON_PASSWORD" > "$PASSWORD_FILE"
        chmod 600 "$PASSWORD_FILE"
        info "Generated new upsmon password"
    fi

    #--------------------------------------------------------------------------
    # STEP 7: Configure TrueNAS UPS service via middleware API
    #--------------------------------------------------------------------------
    info "Configuring TrueNAS UPS service via middleware API"
    ups_payload='{
        "identifier": "'"${UPS_IDENTIFIER}"'",
        "driver": "dummy-ups$US3000",
        "port": "'"${UPS_IDENTIFIER}@localhost:${UPS_PORT}"'",
        "description": "'"${UPS_DESC}"'",
        "monpwd": "'"${UPSMON_PASSWORD}"'",
        "monuser": "upsmon",
        "mode": "MASTER",
        "shutdown": "LOWBATT",
        "shutdowntimer": 30,
        "hostsync": 15,
        "powerdown": false
    }'
    midclt call ups.update "$ups_payload" > /dev/null && info "TrueNAS UPS service configured" \
      || warn "Middleware API call failed — configure UPS service manually in TrueNAS UI"

    # Enable start on boot and start the TrueNAS UPS service
    UPS_SERVICE_ID=$(get_ups_service_id)
    if [ -n "$UPS_SERVICE_ID" ]; then
        midclt call service.update "$UPS_SERVICE_ID" '{"enable": true}' > /dev/null 2>&1 \
            && info "TrueNAS UPS service set to start on boot (id=$UPS_SERVICE_ID)" \
            || warn "Could not enable UPS service — enable manually in TrueNAS UI → Services"
    else
        warn "Could not determine UPS service ID — enable start on boot manually in TrueNAS UI → Services"
    fi
    midclt call service.start '"ups"' '{}' > /dev/null 2>&1 \
        && info "TrueNAS UPS service started" \
        || warn "Could not start UPS service — start manually in TrueNAS UI → Services"

    #--------------------------------------------------------------------------
    # STEP 8: Register init script with TrueNAS (idempotent)
    #--------------------------------------------------------------------------
    info "Registering init script with TrueNAS"
    EXISTING_ID=$(midclt call initshutdownscript.query \
        "[[\"script\", \"=\", \"${INSTALL_PATH}/ugreen-ups-init.sh\"]]" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')" \
        2>/dev/null || true)

    if [ -n "$EXISTING_ID" ]; then
        info "Init script already registered (id=$EXISTING_ID)"
    else
        midclt call initshutdownscript.create "{
            \"type\": \"SCRIPT\",
            \"script\": \"${INSTALL_PATH}/ugreen-ups-init.sh\",
            \"when\": \"POSTINIT\",
            \"enabled\": true,
            \"comment\": \"UGREEN UPS driver init\"
        }" > /dev/null && info "Init script registered with TrueNAS" \
          || warn "Could not register init script — add manually in TrueNAS UI → System → Advanced → Init/Shutdown Scripts"
    fi

    #--------------------------------------------------------------------------
    # STEP 9: Run init script now to bring everything up
    #--------------------------------------------------------------------------
    info "Running init script to bring up UPS driver"
    bash "$INSTALL_PATH/ugreen-ups-init.sh"

    echo ""
    echo "================================================================"
    echo " UGREEN UPS driver installed to: $INSTALL_PATH"
    echo " upsmon password stored at:      $PASSWORD_FILE"
    echo ""
    echo " Verify with:"
    echo "   systemctl status $SERVICE_NAME"
    echo "   upsc ${UPS_IDENTIFIER}@localhost:3494"
    echo "================================================================"
}

###############################################################################
# MAIN
###############################################################################
check_root

case "$ACTION" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    *) error "Unknown action '$ACTION'. Usage: $0 [install|uninstall] [install_path]" ;;
esac
