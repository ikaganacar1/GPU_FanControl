#!/bin/bash
# Run this once: sudo bash setup_sudoers.sh
# Allows gpu_fancontrol to control fans without password prompt

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="${SCRIPT_DIR}/fan_helper.py"
USER="$(logname 2>/dev/null || echo "${SUDO_USER:-ika}")"

# Resolve the real python binary from the user's environment (not sudo's)
# sudo strips PATH, so we check the user's home for conda/venv first
PYTHON_REAL=""
for candidate in \
    "/home/${USER}/miniconda3/bin/python3" \
    "/home/${USER}/anaconda3/bin/python3" \
    "/home/${USER}/.conda/bin/python3" \
    "$(which python3 2>/dev/null)"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
        PYTHON_REAL="$(readlink -f "$candidate")"
        break
    fi
done

if [ ! -f "$HELPER" ]; then
    echo "Error: fan_helper.py not found at ${HELPER}"
    exit 1
fi

if [ ! -f "$PYTHON_REAL" ]; then
    echo "Error: python3 not found"
    exit 1
fi

# Build sudoers rules — include both the real binary and common symlinks
# so it works regardless of which python path sys.executable reports
RULES=""
RULES="${USER} ALL=(root) NOPASSWD: ${PYTHON_REAL} ${HELPER}"

# Also allow common symlink paths that sys.executable might resolve to
PYTHON_DIR="$(dirname "${PYTHON_REAL}")"
for link in python python3 python3.13; do
    LINK_PATH="${PYTHON_DIR}/${link}"
    if [ -f "$LINK_PATH" ] && [ "$LINK_PATH" != "$PYTHON_REAL" ]; then
        RULES="${RULES}
${USER} ALL=(root) NOPASSWD: ${LINK_PATH} ${HELPER}"
    fi
done

# If conda is symlinked (e.g. ~/miniconda3 -> /mnt/.../miniconda3),
# also add the symlink-based paths since sys.executable may report either
CONDA_SYMLINK="/home/${USER}/miniconda3/bin"
if [ -L "/home/${USER}/miniconda3" ] && [ -d "$CONDA_SYMLINK" ]; then
    for link in python python3 python3.13; do
        LINK_PATH="${CONDA_SYMLINK}/${link}"
        if [ -f "$LINK_PATH" ]; then
            RULES="${RULES}
${USER} ALL=(root) NOPASSWD: ${LINK_PATH} ${HELPER}"
        fi
    done
fi

echo "${RULES}" > /etc/sudoers.d/gpu-fancontrol
chmod 0440 /etc/sudoers.d/gpu-fancontrol

echo "Done. Sudoers rules written to /etc/sudoers.d/gpu-fancontrol:"
echo "${RULES}"
echo ""
echo "Test with: sudo -n ${PYTHON_REAL} ${HELPER}"
