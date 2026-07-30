#!/usr/bin/env bash
# Download the YC S19 Demo Day pitch playlist into data/videos/pitches/
# as TwelveLabs-friendly 720p MP4s. Requires yt-dlp:  brew install yt-dlp
#
# Usage:  bash backend/scripts/fetch_pitches.sh [PLAYLIST_URL]
set -euo pipefail

PLAYLIST="${1:-https://www.youtube.com/playlist?list=PLlm1J5MSDEsWzlXhrzqjrwQjQfzZRe12q}"
DEST="$(cd "$(dirname "$0")/../.." && pwd)/data/videos/pitches"
mkdir -p "$DEST"

yt-dlp \
  -f "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b" \
  --merge-output-format mp4 \
  --restrict-filenames \
  -o "$DEST/%(playlist_index)02d_%(title).60s.%(ext)s" \
  "$PLAYLIST"

echo
echo "Downloaded to $DEST:"
ls -lh "$DEST"
echo
echo "Next:  cd backend && uv run python scripts/ingest_pitch.py"
