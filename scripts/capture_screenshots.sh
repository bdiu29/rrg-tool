#!/usr/bin/env bash
#
# Capture screenshots of every page into assets/ using Safari + macOS
# `screencapture` (no extra dependencies). Files are named to match the images
# the READMEs already embed (e.g. the RRG page → chart.png), so each run just
# refreshes the heroes in place. Hand-cropped extras (rotation-calls.png,
# backtest.png) are not page captures and are left untouched.
#
# Usage:
#   1. Start the app in another terminal:   /usr/bin/python3 app.py
#   2. Run this:                            bash scripts/capture_screenshots.sh
#
# First run will prompt for Screen Recording permission for your terminal app
# (System Settings → Privacy & Security → Screen Recording). Grant it, then
# re-run.
#
# Tunables (env vars): PORT, WIDTH, HEIGHT, WAIT, CHROME_TOP (px of browser
# toolbar to crop off the top of each shot).
set -euo pipefail

PORT="${PORT:-8000}"
WIDTH="${WIDTH:-1340}"
HEIGHT="${HEIGHT:-1000}"
WAIT="${WAIT:-6}"          # seconds to let the page (and Plotly) render
CHROME_TOP="${CHROME_TOP:-96}"   # Safari toolbar/tab-bar height to trim
WIN_X=30
WIN_Y=30

BASE="http://localhost:${PORT}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/assets"
mkdir -p "$OUT"

# name | path
# (name = output filename stem; the RRG page writes chart.png to match the README)
PAGES=(
  "home|/|"
  "chart|/rrg.html|"
  "breadth|/breadth.html|"
  "breadth-tape|/breadth.html?view=tape|"
  "schwab|/schwab.html?privacy=1|"
  "screener|/screener.html|"
  "rankings|/rankings.html|"
  "themes|/themes.html|"
  "flow|/flow.html|"
  "canslim|/canslim.html|"
  "news|/news.html|"
  "macro|/macro.html|"
  "harness|/harness.html|"
  "research|/research.html|"
)

if ! curl -s -o /dev/null --max-time 3 "${BASE}/"; then
  echo "✗ App not reachable at ${BASE}. Start it first:  /usr/bin/python3 app.py" >&2
  exit 1
fi

capture() {
  local name="$1" url="$2" out="${OUT}/${name}.png"

  osascript >/dev/null <<APPLESCRIPT
tell application "Safari"
  activate
  if (count of windows) = 0 then
    make new document
  end if
  set URL of front document to "${url}"
  set bounds of front window to {${WIN_X}, ${WIN_Y}, ${WIN_X}+${WIDTH}, ${WIN_Y}+${HEIGHT}}
end tell
delay ${WAIT}
APPLESCRIPT

  # window region in screen points (AppleScript bounds = {x1,y1,x2,y2})
  read -r x1 y1 x2 y2 < <(osascript -e 'tell application "Safari" to get bounds of front window' | tr ',' ' ')
  local cx=$x1 cy=$(( y1 + CHROME_TOP )) cw=$(( x2 - x1 )) ch=$(( y2 - y1 - CHROME_TOP ))
  screencapture -x -o -R "${cx},${cy},${cw},${ch}" "$out"
  echo "  ✓ ${name}.png"
}

echo "Capturing ${#PAGES[@]} pages from ${BASE} → assets/"
for p in "${PAGES[@]}"; do
  IFS='|' read -r name path <<< "$p"
  capture "$name" "${BASE}${path}"
done
echo "Done. Re-run any time to refresh."
