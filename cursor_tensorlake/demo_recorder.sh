#!/usr/bin/env bash
# Film the sandbox desktop while a Cursor agent drives it.
#
# This script only watches. It never moves the pointer, presses a key, or opens
# a window: the agent's own computer use does all of that. Its job is to leave
# evidence behind, because Cursor's screenshots go to the model and not to disk.
#
#   demo_recorder.sh [OUT_DIR] [DISPLAY]
#
# With no DISPLAY it picks the one that has a browser window, so it films the
# right desktop whether the worker attached to the display the image already
# runs or started its own.
#
# The video is fragmented MP4: every fragment is playable on its own, so the
# file survives a sandbox that is suspended or killed mid-recording.

set -uo pipefail

OUT="${1:-/home/tl-user/demo-artifacts}"
WANTED_DISPLAY="${2:-}"
FPS="${RECORD_FPS:-10}"
STILL_EVERY="${STILL_EVERY:-10}"

mkdir -p "$OUT"

displays() {
  ls /tmp/.X11-unix 2>/dev/null | sed -n 's/^X\([0-9]\{1,\}\)$/:\1/p' | sort -V
}

has_browser() {
  DISPLAY="$1" xdotool search --onlyvisible --class '(chrome|chromium|firefox)' 2>/dev/null | grep -q .
}

pick_display() {
  local found=""
  local d
  for d in $(displays); do
    if [ -z "$found" ]; then found="$d"; fi
    if has_browser "$d"; then echo "$d"; return 0; fi
  done
  # No browser yet: the agent has not opened one. Wait a while for it rather
  # than film the wrong desktop, then settle for the first display.
  local waited=0
  while [ "$waited" -lt "${BROWSER_WAIT:-90}" ]; do
    sleep 3
    waited=$((waited + 3))
    for d in $(displays); do
      if has_browser "$d"; then echo "$d"; return 0; fi
    done
  done
  echo "$found"
}

DISP="${WANTED_DISPLAY:-$(pick_display)}"
if [ -z "$DISP" ]; then
  echo "no X display found under /tmp/.X11-unix" >&2
  exit 1
fi

GEOMETRY="$(DISPLAY="$DISP" xdpyinfo 2>/dev/null | awk '/dimensions:/{print $2; exit}')"
GEOMETRY="${GEOMETRY:-1280x800}"

{
  echo "display=$DISP"
  echo "geometry=$GEOMETRY"
  echo "fps=$FPS"
  echo "started=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "displays_present=$(displays | tr '\n' ' ')"
} > "$OUT/recording.txt"

# The stopper reads this to signal the script itself, so ffmpeg gets a SIGINT
# and closes the file properly instead of being killed outright.
echo $$ > "$OUT/recorder.pid"

echo "recording $DISP ($GEOMETRY) at ${FPS}fps into $OUT" >&2

# Periodic stills. A contact sheet is easier to skim than the video, and one of
# these frames is usually the screenshot worth showing.
stills() {
  local n=0
  while :; do
    n=$((n + 1))
    ffmpeg -nostdin -loglevel error -f x11grab -video_size "$GEOMETRY" -i "$DISP" \
      -frames:v 1 -y "$(printf '%s/still-%03d.png' "$OUT" "$n")" 2>/dev/null
    sleep "$STILL_EVERY"
  done
}
stills &
STILLS_PID=$!

# A keyframe every few seconds, because each fragment is only written out at
# one. Without it the file stays empty-looking for the first half-minute and
# loses everything since the last keyframe if the recorder is killed outright.
KEYFRAME_EVERY=$((FPS * 3))

ffmpeg -nostdin -loglevel warning \
  -f x11grab -framerate "$FPS" -video_size "$GEOMETRY" -i "$DISP" \
  -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2' \
  -c:v libx264 -preset veryfast -crf 30 -pix_fmt yuv420p -g "$KEYFRAME_EVERY" \
  -movflags +frag_keyframe+empty_moov \
  -y "$OUT/session.mp4" &
FFMPEG_PID=$!

# SIGINT lets ffmpeg close the file properly. The fragmented MP4 above is the
# fallback for when nothing gets to run at all.
finish() {
  kill -INT "$FFMPEG_PID" 2>/dev/null
  kill "$STILLS_PID" 2>/dev/null
  wait "$FFMPEG_PID" 2>/dev/null
  echo "stopped=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUT/recording.txt"
  rm -f "$OUT/recorder.pid"
  chmod -R a+rX "$OUT" 2>/dev/null
  exit 0
}
trap finish TERM INT

wait "$FFMPEG_PID"
kill "$STILLS_PID" 2>/dev/null
