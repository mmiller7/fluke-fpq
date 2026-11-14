#!/bin/bash

# This script created by hand to add proper udev rules and driver module for Fluke VR1710 USB-Serial converter

# Created by: Matthew Miller

set -e

if [[ ${EUID} -ne 0 ]]; then
    echo "This script must be run as root" >&2
    exit 1
fi

echo ""

echo "Loading cp210x kernel module . . ."
modprobe cp210x
echo 0f7e 9003 > /sys/bus/usb-serial/drivers/cp210x/new_id



if [[ "$1" == "install" ]]; then
    echo "Installing udev rule . . ."

    cat > "${UDEV_RULE}" <<'_EOF'
#Fluke VR1710 Voltage Quality Recorder
#File: /etc/udev/rules.d/99-fluke_vr1710.rules
#Force loading cp210x module
#Add new custom device ID to module driver

ACTION=="add", ATTRS{idVendor}=="0f7e", ATTRS{idProduct}=="9003", RUN+="/sbin/modprobe cp210x" RUN+="/bin/sh -c 'echo 0f7e 9003 > /sys/bus/usb-serial/drivers/cp210x/new_id'"
_EOF

else
    echo "Option \"install\" not specified, skipping persistent udev rule installation."
    echo "To install the udev rule, run \"$(basename "$0") install\""
fi

echo "Done."
