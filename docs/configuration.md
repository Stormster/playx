# Configuration

PlayX reads its settings from `.env` next to `docker-compose.yml` (`setup.sh` creates it from
`.env.example`). After changing `.env`, run `docker compose up -d` again (with the same `--profile`
flags you use) to apply it. Blank means unset.

## Basics

| Setting | Default | What it does |
| --- | --- | --- |
| `XP_ADMIN_TOKEN` | none | Unlocks `/admin`. Without it the admin page is off. Use a long random string. |
| `PUID`, `PGID` | `99`, `100` | User and group the game runs as. They must own `data/`. |

## Public address and identity

| Setting | Default | What it does |
| --- | --- | --- |
| `XP_ORIGIN` | the request's host | Public address of the page, e.g. `https://play.example.com`. The X card needs it for absolute links. |
| `TUNNEL_TOKEN` | none | Cloudflare Tunnel token, used with `--profile tunnel`. |
| `XP_X_HANDLE` | none | Your X account (with or without `@`), credited on the card as `twitter:site`. |
| `XP_FOOTER_TEXT` | `PlayX` | The small link on the handheld. Text after the last `.` gets the accent colour (`example.dev`). |
| `XP_FOOTER_URL` | this project on GitHub | Where that link goes. Must start with `http://` or `https://`. |
| `XP_REDIRECTS` | none | Old hostnames to send to new ones with a 301, as `old=new,old2=new2`. Only active once `XP_ORIGIN` is one of the new hostnames. |

## Admin

| Setting | Default | What it does |
| --- | --- | --- |
| `XP_ADMIN_HOST` | none (any host) | Hostname(s) for the admin page, comma-separated. When set, `/admin` and `/stats` answer only there and only with a Cloudflare Access login header; everywhere else they return 404. The header is checked for presence, not verified, so this relies on port 8787 being reachable only through the tunnel. `*` means any host. See [deploy.md](deploy.md#3-lock-down-the-admin-page). |

Everything else is set on the admin page and saved to `data/settings.json`: theme, game speed, pause,
vote window, mode lock, run-clock idle timer, empty-room timer, release guard, simulated players,
step multipliers and unlimited Anarchy holds. A forced mode lasts until the next restart.

## Video

| Setting | Default | What it does |
| --- | --- | --- |
| `XP_R2_BUCKET` | none | Turns on R2 chunks. When set, the four below are required too. Unset = chunks are served by the game server at `/cdn`. |
| `XP_R2_ACCOUNT_ID` | none | Your Cloudflare account ID (R2 overview page). |
| `XP_R2_ACCESS_KEY_ID`, `XP_R2_SECRET_ACCESS_KEY` | none | An R2 API token with Object Read & Write on the bucket. |
| `XP_R2_PUBLIC_BASE` | none | The bucket's public address, e.g. `https://video.example.com`. |
| `XP_CHUNK_TTL` | `300` | Seconds before an uploaded chunk is deleted. |
| `XP_RELAY_URL` | none | The live relay's address. Turns on the relay. |
| `XP_RELAY_SECRET` | none | Must match the relay's `PUB_SECRET`. Required with `XP_RELAY_URL`. |
| `XP_RUN_NAME` | `firered` | Name of the run in chunk paths and on the relay. Change it if one bucket or relay serves several games. |
| `XP_VIDEO` | `chunks` | `chunks` is the normal path. Anything else sends video over the vote socket instead (testing only; no relay or R2). |

## Relay (`relay/wrangler.jsonc`)

| Setting | What it does |
| --- | --- |
| `PUB_SECRET` (Worker secret) | Set with `npx wrangler secret put PUB_SECRET`. Only the game server, holding this, can publish video. |
| `ALLOWED_ORIGINS` (var) | Page origins allowed to watch, comma-separated. Empty = any. |
| `routes` | Optional custom domain instead of `*.workers.dev`. |

## Tuning in code

Vote and timing rules are constants at the top of `server/run.py`, with a comment on each. Among
them: the 60% share and 10 s hold needed to switch modes, the 10-minute mode lock, the per-IP vote cap,
Anarchy burst limits, Start and Select needing twice the runner-up's votes, autosave and rollback
intervals, the release guard's 20 s rewind, and the 30 s empty-room grace period.
