#!/bin/bash
# Run this once: sudo bash setup_sudoers.sh
# Allows gpu_fancontrol to control fans without password prompt

PYTHON="/home/ika/miniconda3/bin/python3"
HELPER="/home/ika/gpu_control/fan_helper.py"

echo "ika ALL=(root) NOPASSWD: ${PYTHON} ${HELPER}" > /etc/sudoers.d/gpu-fancontrol
chmod 0440 /etc/sudoers.d/gpu-fancontrol

echo "Done. Fan control helper can now run without password."
echo "Test with: sudo -n ${PYTHON} ${HELPER}"
