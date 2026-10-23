#!/bin/bash
# Build Amber's PHY, OAI's C library libaiot_phy.so (openair1/PHY/AIOT), into amber/phy/lib/.
#
#   scripts/build_aiot_lib.sh                # fetch the library's sources from OAI_REPO at OAI_REF
#   scripts/build_aiot_lib.sh /path/to/oai   # or build from an OAI checkout
#
# Needs git, cmake, a C compiler and SIMDe (Ubuntu: libsimde-dev), which OAI's coders use.
set -euo pipefail
OAI_REPO=${OAI_REPO:-https://github.com/nayreed/openairinterface5g.git}
OAI_REF=${OAI_REF:-aiot-rfsim}
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=$HERE/amber/phy/lib
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [ $# -ge 1 ]; then
  SRC=$(cd "$1" && pwd)
else
  # only the library and the OAI files it builds on: OAI's CRC, LTE convolutional coder and Viterbi
  # decoder, and the headers they include
  git clone -q --depth 1 --filter=blob:none --sparse --branch "$OAI_REF" "$OAI_REPO" "$WORK/oai"
  git -C "$WORK/oai" sparse-checkout set --no-cone /openair1/PHY/AIOT/ \
    /openair1/PHY/CODING/crc_byte.c /openair1/PHY/CODING/ccoding_byte_lte.c /openair1/PHY/CODING/viterbi_lte.c \
    /openair1/PHY/CODING/coding_defs.h /openair1/PHY/CODING/crc.h /openair1/PHY/CODING/crcext.h \
    /openair1/PHY/CODING/types.h /openair1/PHY/sse_intrin.h /common/platform_types.h \
    /common/utils/assertions.h /common/utils/utils.h /common/utils/ds/byte_array.h /common/utils/nr/nr_common.h
  SRC=$WORK/oai
fi

cmake -S "$SRC/openair1/PHY/AIOT" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DAIOT_BUILD_TESTS=OFF > /dev/null
cmake --build "$WORK/build" --target aiot_phy_shared -j "$(nproc 2>/dev/null || echo 2)" > /dev/null
mkdir -p "$OUT"
cp "$WORK/build/libaiot_phy.so" "$OUT/"
echo "built $OUT/libaiot_phy.so"
