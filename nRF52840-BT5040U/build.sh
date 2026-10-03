#!/bin/bash
# Build the dongle firmware with the nRF Connect SDK installed by nRF Connect for Desktop.
# Usage: ./build.sh            (output: build/merged.hex)
set -euo pipefail

NCS=${NCS:-/opt/nordic/ncs/v3.2.1}
TOOLCHAIN=${TOOLCHAIN:-$(ls -d /opt/nordic/ncs/toolchains/*/ | head -1)}
TOOLCHAIN=${TOOLCHAIN%/}
HERE=$(cd "$(dirname "$0")" && pwd)

export PATH="$TOOLCHAIN/bin:$TOOLCHAIN/usr/bin:$TOOLCHAIN/usr/local/bin:$TOOLCHAIN/opt/bin:$TOOLCHAIN/opt/nanopb/generator-bin:$TOOLCHAIN/nrfutil/bin:$TOOLCHAIN/opt/zephyr-sdk/arm-zephyr-eabi/bin:$PATH"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$TOOLCHAIN/opt/zephyr-sdk"
export ZEPHYR_BASE="$NCS/zephyr"

# Build stamp reported by the dongle's INFO command: <UTC build time>-<git commit>[-dirty]
COMMIT=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo nogit)
git -C "$HERE" diff --quiet HEAD -- . 2>/dev/null || COMMIT="$COMMIT-dirty"
VERSION="$(date -u +%Y%m%d-%H%M)-$COMMIT"

cd "$NCS"
west build -p auto -b nrf52840dongle/nrf52840 -d "$HERE/build" "$HERE" -- -DPIHUB_FW_VERSION="$VERSION"
echo "Built $HERE/build/merged.hex (firmware $VERSION)"
