#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  exec uv run tango-posture gui
fi
if [[ "$1" == "--cli" ]]; then
  shift
fi

VIDEOS_DIR="videos"
mkdir -p "$VIDEOS_DIR"

max_num=0
shopt -s nullglob
for file in "$VIDEOS_DIR"/practice_[0-9]*.mp4 "$VIDEOS_DIR"/overlay_[0-9]*.mp4; do
  base="$(basename "$file" .mp4)"
  num_str="${base##*_}"
  if [[ "$num_str" =~ ^[0-9]+$ ]]; then
    num=$((10#$num_str))
    if (( num > max_num )); then
      max_num=$num
    fi
  fi
done
shopt -u nullglob

next_num=$((max_num + 1))
next_id="$(printf "%02d" "$next_num")"

raw_video="${VIDEOS_DIR}/practice_${next_id}.mp4"
overlay_video="${VIDEOS_DIR}/overlay_${next_id}.mp4"

echo "Starting run #${next_id}:"
echo "  Raw video:     ${raw_video}"
echo "  Overlay video: ${overlay_video}"

exec uv run tango-posture run \
  --camera 0 \
  --side left \
  --facing right \
  --record-video "$raw_video" \
  --record-overlay "$overlay_video" \
  "$@"
