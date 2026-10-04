#!/bin/bash
# Render the trailer into demo/. Usage: ./render.sh [en] [ru]   (default: both)
# Needs Node.js 22+, FFmpeg and Chrome; set HYPERFRAMES_BROWSER_PATH to a chrome-headless-shell for speed.
set -euo pipefail
cd "$(dirname "$0")"
langs=("$@")
[ ${#langs[@]} -eq 0 ] && langs=(en ru)
for l in "${langs[@]}"; do
  raw="../.raw-$l.mp4"
  npx --yes hyperframes@0.8.125 render --quality delivery --variables "{\"lang\":\"$l\"}" --output "$raw"
  # the delivery encode is ~100 MB; CRF 27 keeps the look at ~12 MB
  ffmpeg -v error -y -i "$raw" -c:v libx264 -preset slow -crf 27 -pix_fmt yuv420p -movflags +faststart \
    -c:a aac -b:a 160k "../telegram-harness-trailer-$l.mp4"
  rm -f "$raw"
done
