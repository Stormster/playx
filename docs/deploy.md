# Going live

The quick start in the [README](../README.md) gets the game running on `localhost`. This page takes it
to a public address you can post on X, then covers the optional pieces for bigger crowds.

| Step | Needed? | Cost |
| --- | --- | --- |
| [1. Run the server](#1-run-the-server) | Yes | Your machine |
| [2. Give it a public address](#2-give-it-a-public-address) | Yes, to share it | Free (Cloudflare Tunnel) |
| [3. Lock down the admin page](#3-lock-down-the-admin-page) | Strongly recommended | Free (Cloudflare Access) |
| [4. Post it on X](#4-post-it-on-x) | Yes, for the X card | Free |
| [5. Video chunks on R2](#5-video-chunks-on-r2) | For crowds your upload can't serve | Free tier, no egress fees |
| [6. Live relay](#6-live-relay) | For ~0.3 s video instead of ~2 s | Workers free plan, $5/month past it |
| [7. Automatic updates](#7-automatic-updates) | Optional | Free |

The examples use `play.example.com` for the game, `admin.example.com` for the admin page,
`video.example.com` for R2 and `live.example.com` for the relay. Use your own domain. Cloudflare
steps 2, 3 and 5 need the domain's DNS on Cloudflare (a free account is enough).

## 1. Run the server

Any always-on machine with Docker works: a home server, a NAS, a small VPS. The emulator only runs
while someone is watching, so an empty room costs almost nothing.

```bash
git clone https://github.com/Stormster/playx.git && cd playx
./setup.sh ~/path/to/your-game.gba
docker compose up -d
docker compose logs -f playx       # expect: video: chunks -> LocalSink
```

What `setup.sh` does, if you'd rather do it by hand:

1. Creates `roms/` and `data/`, and copies your ROM to `roms/game.gba`.
2. Copies `.env.example` to `.env`, puts a random `XP_ADMIN_TOKEN` in it, and sets `PUID`/`PGID` to
   your user (when run as root, to `99:100`, the image's built-in user, and it gives `data/` to that
   user).

The container runs as `PUID:PGID` and must be able to write `data/`. If the log says `Permission
denied` on `/data`, fix the owner: `sudo chown -R 99:100 data` (or your `PUID:PGID`).

**Saves** live in `data/`: every 10 s in memory, every minute on disk (3 hours kept), hourly forever,
and on every stop. Back up that folder and you have the whole run.

**Building the image yourself** instead of pulling `ghcr.io/stormster/playx`: uncomment `build: .`
in `docker-compose.yml` and run `docker compose up -d --build`. Leave out the autoupdate profile then, or Watchtower
replaces your build with the published image. The published image is built for
amd64 and arm64 (for example a Raspberry Pi or an Apple silicon Mac).

## 2. Give it a public address

The X card and every viewer need HTTPS. A Cloudflare Tunnel gives you that without opening ports or
exposing your home IP:

1. Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel** →
   Cloudflared → name it (e.g. `playx`) → choose **Docker**. Copy the token from the command it
   shows (the long string after `--token`).
2. Put it in `.env` as `TUNNEL_TOKEN=...`.
3. In the tunnel's **Public hostname** tab, add `play.example.com` → service `http://playx:8787`.
4. Set `XP_ORIGIN=https://play.example.com` in `.env`. The X card needs it to build absolute links.
5. Start with the tunnel:
   ```bash
   docker compose --profile tunnel up -d
   ```

Open `https://play.example.com`. You should see the game.

**Another reverse proxy** (Caddy, nginx, Traefik) works too. It must pass WebSockets on `/ws` and set
`X-Forwarded-For`. PlayX trusts that header (and `CF-Connecting-IP`) for its per-IP vote limits, so
**don't expose port 8787 directly to the internet**. Compose publishes it on `127.0.0.1` only; a
proxy on the same machine can use that, and one in Docker can reach `http://playx:8787`.

## 3. Lock down the admin page

Out of the box, `/admin` on every address asks only for `XP_ADMIN_TOKEN`. Once the game is public,
add a login in front of it and keep it off the public hostname:

1. In the tunnel, add a second public hostname: `admin.example.com` → `http://playx:8787`.
2. **Zero Trust** → **Access** → **Applications** → **Add an application** → Self-hosted, domain
   `admin.example.com`. Add a policy that allows only your email (one-time PIN login works without
   any other setup).
3. Set `XP_ADMIN_HOST=admin.example.com` in `.env` and run `docker compose --profile tunnel up -d`.

Now `/admin` answers only on `admin.example.com`, only after the Cloudflare login, and still asks for
the token. Everywhere else it returns 404. This holds as long as port 8787 is reachable only through
the tunnel: PlayX checks that the Access header is present but doesn't verify it, so keep the
`127.0.0.1` in the compose `ports:` line.

## 4. Post it on X

Post `https://play.example.com` (the page itself, not `/embed`). X reads the card tags on that page
and shows the handheld inside the post (`/embed`) where it can, or the card image linking to the page
where it can't.

- Set `XP_X_HANDLE` to your X account so the card credits it.
- Replace `web/social/firered-play-v1.png` and the titles in `web/index.html` if you're not running
  FireRed.
- X caches cards. After changing anything, post the URL with a new query string, e.g. `?v=2`.
- X meant Player Cards for audio and video, and has limited which sites get them in the past. Check
  how your post looks on x.com and in the phone apps before you announce it.

## 5. Video chunks on R2

Without R2 your machine serves the video itself, one copy per viewer, so your upload speed limits
the crowd. With R2 your machine uploads each ~2 s chunk once and Cloudflare
serves everyone, with no egress fees. Chunks only upload while someone is watching, and each is
deleted 5 minutes later.

1. **R2** → **Create bucket** (e.g. `playx`).
2. Bucket → **Settings**:
   - **Custom domain**: `video.example.com`.
   - **Object lifecycle rule**: delete objects after 1 day. This is only a backstop for crashes; the
     server cleans up after itself.
   - **CORS policy**:
     ```json
     [{"AllowedOrigins": ["https://play.example.com"], "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400}]
     ```
3. **Caching** → **Cache Rules** on your domain: hostname equals `video.example.com` → eligible for
   cache, respect origin cache-control.
4. **R2** → **Manage API tokens** → create a token with **Object Read & Write** on that bucket only.
5. In `.env`:
   ```bash
   XP_R2_BUCKET=playx
   XP_R2_ACCOUNT_ID=...            # shown on the R2 overview page
   XP_R2_ACCESS_KEY_ID=...
   XP_R2_SECRET_ACCESS_KEY=...
   XP_R2_PUBLIC_BASE=https://video.example.com
   ```
6. `docker compose up -d` again. The log should say `video: chunks -> R2Sink`.

## 6. Live relay

The relay is a Cloudflare Worker with a Durable Object. Your machine sends it one live stream and it
fans that out to every viewer about 0.3 s behind the game, instead of ~2 s with chunks. If the relay
is down or over its limits, viewers fall back to chunks on their own and come back when it recovers.

```bash
cd relay
npm install
npx wrangler login
npx wrangler secret put PUB_SECRET     # any long random string, e.g. from: openssl rand -hex 24
npx wrangler deploy                    # prints https://playx-relay.<you>.workers.dev
```

In `.env`, set `XP_RELAY_URL` to that address and `XP_RELAY_SECRET` to the same string, then
`docker compose up -d`. The log should say `relay: connected`, and the admin page shows which path
viewers are on (`relay`, `relay + R2 for N viewer(s)`, or `relay down, R2 chunks`).

Optional, in `relay/wrangler.jsonc`:

- `ALLOWED_ORIGINS`: set it to `https://play.example.com` so other sites can't embed your stream.
- `routes`: serve it on your own domain (`live.example.com`) instead of workers.dev.

To keep your domain out of your git history (for example in a fork), copy the file to
`relay/wrangler.local.jsonc`, which git ignores, and deploy with
`npx wrangler deploy -c wrangler.local.jsonc`.

The Workers free plan allows about 100k viewer connections a day, and one relay feeds a few thousand
viewers. Past that, viewers drop to R2 automatically. The $5/month Workers plan removes the daily cap.
Relay changes deploy by hand with `npx wrangler deploy`; updating the game server doesn't touch it.

## 7. Automatic updates

```bash
docker compose --profile tunnel --profile autoupdate up -d
```

Watchtower checks for a new image every 5 minutes and restarts the game on it. The game saves on
stop and resumes from that save, so an update costs viewers a few seconds of stream.

**Running your own fork:** merges to `main` build `ghcr.io/<your-user>/<repo>` with GitHub Actions
(`.github/workflows/build.yml`). Point `image:` in `docker-compose.yml` at it. On GitHub, open the
package → **Package settings** → make it public, or run `docker login ghcr.io` on the server with a
token that has the `read:packages` scope.

## Troubleshooting

- **`docker compose logs -f playx`** shows the video path at start (`video: chunks -> ...`,
  `relay: connected`) and any errors.
- **In a viewer's browser console**, `PlayXVideo()` shows which video path that viewer is on and its
  delay.
- **Black screen with no viewers:** the game freezes 30 s after the room empties and wakes when
  someone joins. A paused game (admin page) stays paused.
- **`R2 is enabled but missing: ...`** at start: `XP_R2_BUCKET` is set but another `XP_R2_*` value
  isn't.
- **`/admin` returns 404:** `XP_ADMIN_HOST` is set and you're on a different hostname, or the request
  didn't come through Cloudflare Access.
