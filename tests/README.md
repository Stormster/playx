The checks use a fake emulator, disposable storage, and local endpoints. They do not access the
real game, ROM, R2 bucket, or deployed relay.

The Python and browser suites also run on pull requests in `.github/workflows/test.yml`.
Reliability checks cover duplicate mode voters, the IP cap, disk-write failures and recovery,
preserving complete save files, and emulator health while running, paused, or stopped.

With the server dependencies from `Dockerfile` installed:

```bash
python -m unittest discover -s tests -v
```

For the relay protocol check, start the local Worker in another terminal:

```bash
cd relay
npm ci
npx wrangler dev --local --port 8799 --var PUB_SECRET:local-test-only --var ALLOWED_ORIGINS:http://127.0.0.1:8787
```

Then run `python tests/relay-protocol.py`. It checks publisher auth, viewer origins, forwarding,
and the current frozen picture for late joiners.

For the browser recovery checks, serve `web/` on localhost port 8789, then use a Playwright
installation with Chromium. The page uses injected test WebSockets; no real game server is needed.

```bash
python -m http.server 8789 --bind 127.0.0.1 --directory web
# In another terminal, from the repo root:
npm install --prefix /tmp/playx-browser-validation playwright
/tmp/playx-browser-validation/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/playx-browser-validation/node_modules node tests/video-browser.cjs
PLAYX_WEB_URL=http://127.0.0.1:8789/embed.html NODE_PATH=/tmp/playx-browser-validation/node_modules node tests/mode-browser.cjs
NODE_PATH=/tmp/playx-browser-validation/node_modules node tests/keyboard-browser.cjs
NODE_PATH=/tmp/playx-browser-validation/node_modules node tests/play-layout-browser.cjs
```

`PLAYX_BROWSER_EXECUTABLE` can select an existing Chromium executable. The checks cover a relay
that never sends its first picture, resumed play on a stalled relay, recovery reporting, and a
slow chunk fetch finishing after switching to relay.
Reconnect checks cover restarted chunk numbers, replaying a frozen chunk, stale fetch/decode
completion, and establishing a fresh relay connection.
The mode checks cover stable percentages at 320/480 px, immediate lock resets, pixel announcements,
sound-cue dismissal and reduced motion. Lifecycle checks cover watched animation without votes,
empty-room freeze/rejoin, host pause, clock accounting, and mode-lock duration changes.
Keyboard checks cover arrows and WASD, other game keys, held-key repeats in page and iframe
at 320/480 px, Democracy vote renewal, Anarchy burst limits and cooldowns, alias keys,
focus/visibility/disconnection cancellation, normal text and shortcut behavior, and keyboard help.
The Python input checks also cover server-enforced Anarchy limits shared across tabs and
reconnections, changed identities, bounded per-IP inputs, and expiry of old limiter records.
Unlimited Anarchy hold checks cover the protected admin toggle, persisted settings and initial/live
viewer updates, long holds, toggle-off cancellation, and retained rate/IP/queue limits.
Simulated-player checks cover protected integer counts from 0 to 50, default-off/restored settings,
bounded input cadence, both voting modes, real viewer/mode-voter isolation, and pause/empty-room
cleanup. Browser checks cover the disclosed bot count, draft editing during polling, Apply/Off,
invalid values, failed saves, retry and reload.
Expanded play checks cover desktop and phone portrait/landscape layouts across all themes,
viewport fit, screen aspect ratio, a larger feed picture, comfortable portrait controls,
top-bar sound placement, active versus preferred mode,
and Chromium fullscreen entry, exit, denial and unsupported-browser fallback. Optional
`PLAYX_PREVIEW_FRAME` and `PLAYX_LAYOUT_SHOTS` supply a captured still and screenshot output folder
for local visual review; the fixture blocks external traffic and keeps all inputs in fake sockets.
