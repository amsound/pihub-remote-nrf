#!/bin/bash
# Build the dongle firmware with the nRF Connect SDK installed by nRF Connect for Desktop.
# Usage: ./build.sh            (output: build/merged.hex)
#        ./build.sh 11ms       (11.25 ms interval experiment, output: build-11ms/merged.hex)
set -euo pipefail

NCS=${NCS:-/opt/nordic/ncs/v3.2.1}
TOOLCHAIN=${TOOLCHAIN:-$(ls -d /opt/nordic/ncs/toolchains/*/ | head -1)}
TOOLCHAIN=${TOOLCHAIN%/}
HERE=$(cd "$(dirname "$0")" && pwd)

export PATH="$TOOLCHAIN/bin:$TOOLCHAIN/usr/bin:$TOOLCHAIN/usr/local/bin:$TOOLCHAIN/opt/bin:$TOOLCHAIN/opt/nanopb/generator-bin:$TOOLCHAIN/nrfutil/bin:$TOOLCHAIN/opt/zephyr-sdk/arm-zephyr-eabi/bin:$PATH"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$TOOLCHAIN/opt/zephyr-sdk"
export ZEPHYR_BASE="$NCS/zephyr"

VARIANT=${1:-}
case "$VARIANT" in
  "")   OUT="$HERE/build";      EXTRA=() ;;
  11ms) OUT="$HERE/build-11ms"; EXTRA=(-DEXTRA_CONF_FILE="$HERE/interval-11ms.conf") ;;
  *)    echo "unknown variant '$VARIANT' (use: ./build.sh  or  ./build.sh 11ms)"; exit 1 ;;
esac

cd "$NCS"
# --cmake: configure every time, so the build stamp is always current.
west build -p auto --cmake -b nrf52840dongle/nrf52840 -d "$OUT" "$HERE" ${EXTRA[@]+-- "${EXTRA[@]}"}
echo "Built $OUT/merged.hex (firmware $(cat "$OUT"/*/pihub_fw_version.txt 2>/dev/null || echo unknown))"
