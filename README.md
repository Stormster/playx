# PlayX

**One Game Boy Advance game, played by everyone watching, live inside an X post.**

**[▶ Play the live game](https://playx.stormster.dev)**: Pokémon FireRed, with everyone else who's on.

Post a link on X and the post becomes a handheld. Everyone who taps play controls the same game:
they vote on the next button, and the crowd's choice goes in. Think Twitch Plays Pokémon, but in
the timeline, with no stream software and no chat bot. It was built for Pokémon FireRed and runs
any GBA game you own.

<p align="center"><a href="https://playx.stormster.dev"><img src="web/social/firered-play-v1.png" alt="Play the live game: a red handheld showing Pokémon FireRed, with 'Tap to play'" width="320"></a></p>

- **Democracy and Anarchy.** In Democracy the most-voted button wins each round. In Anarchy every
  press goes in, with limits per player. The crowd votes to switch modes.
- **Runs on your own machine.** A home server, NAS or small VPS runs the emulator. Viewers only ever
  get video, never the ROM.
- **Hard to wreck.** Autosaves every minute, rollback to any point in the last 3 hours from the admin
  page, and (in FireRed) releasing Pokémon gets undone automatically.
- **Scales cheaply.** Small crowds are served straight from your machine. For big ones, video can go
  through Cloudflare (R2 and a Workers relay), so your upload stays at one stream however many watch.
- **Works outside X too.** The same page plays in any browser, full screen or embedded.

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) and a GBA ROM you own.

```bash
git clone https://github.com/Stormster/playx.git && cd playx
./setup.sh ~/path/to/your-game.gba     # folders, .env, admin token
docker compose up -d
```

Open <http://localhost:8787> to play and <http://localhost:8787/admin> for the host controls (the
admin token is printed by `setup.sh` and saved in `.env`). On your own, you are the whole crowd:
your button presses go straight in.

**To put it on X**, the game needs a public HTTPS address. [docs/deploy.md](docs/deploy.md) walks
through it with a free Cloudflare Tunnel (no ports to open), then the optional extras for big crowds.

## How it works

```
viewers ──votes (tiny text)───────────▶ public address (e.g. Cloudflare Tunnel) ──▶ your machine
viewers ◀──video (from your machine)─── same address ◀──────────────────────────── PlayX container
                                                                        (emulator, votes, saves)
optional, for big crowds:
viewers ◀──live video (~0.3 s)── relay (Cloudflare Worker) ◀── one stream from your machine
viewers ◀──video chunks (~2 s)── Cloudflare R2 + cache    ◀── uploads from your machine
```

One container runs the emulator ([mGBA](https://mgba.io)'s libretro core), counts votes, saves, and
serves the page. Video goes out as short chunks plus, if you set up the relay, a live stream about
0.3 s behind the game. Viewers switch between the two on their own when one has trouble.

The game keeps running while anyone is watching and freezes 30 s after the last viewer leaves, so an
empty room costs nothing. Restarts and updates save first and resume from that save, so viewers lose
a few seconds of stream and no game progress.

## The admin page

At `/admin`, unlocked with `XP_ADMIN_TOKEN`:

- Pause, game speed 1x to 4x, and a theme for everyone (Flame Red, Cobalt, Graphite, Pearl, PlayX lime).
- Force Democracy or Anarchy, the vote window, and how long a mode switch locks.
- Rollback with thumbnails: every minute for 3 hours, then hourly forever.
- The run clock (play time, counted only while people are playing) and the empty-room timer.
- The release guard, and simulated players (bots that press buttons) to fill a quiet room.

Settings survive restarts. Before going public, put the admin page behind a login: see
[docs/deploy.md](docs/deploy.md#3-lock-down-the-admin-page).

## Other games

Any GBA ROM runs. Two features read Pokémon FireRed (US v1.0) memory and should be off for anything
else:

- **Release guard** (on by default): rewinds 20 s if the Pokémon count drops. Turn it off in the
  admin page for other games, or it can rewind on random memory changes.
- **Name filter** (`server/names.py`): replaces rude trainer and Pokémon names. Off unless you
  enable it in code.

The page copy and the X card image (`web/index.html`, `web/social/`) mention Pokémon. Change them
for your game.

## Docs

| | |
| --- | --- |
| [docs/deploy.md](docs/deploy.md) | Going live: public address, admin login, posting on X, R2, relay, updates |
| [docs/configuration.md](docs/configuration.md) | Every setting in `.env` |
| [tests/README.md](tests/README.md) | Running the test suites |

## Repo

| Path | What |
| --- | --- |
| `server/` | Run server: emulator, vote room, saves, release guard, run clock, video publisher, admin API |
| `web/` | The handheld page: full page, `/play` (full screen) and `/embed` (inside the X card) |
| `relay/` | Optional live relay: a Cloudflare Worker and Durable Object that fan the video out |
| `Dockerfile` | Builds mGBA's libretro core and the server image. The ROM is never in the image. |
| `docker-compose.yml` | The game, plus optional Cloudflare Tunnel and Watchtower (auto-updates) |
| `setup.sh` | First-time setup |
| `tests/` | Python and browser tests, all with a fake emulator (no ROM needed) |

## Development

```bash
pip install numpy==2.1.3 aiohttp==3.10.11 pillow==11.0.0
python3 server/run.py --rom path/to/game.gba --core path/to/mgba_libretro.so --data ./data/dev
python -m unittest discover -s tests -v
```

The libretro core comes from the Docker build (`/app/core/mgba_libretro.so`) or your own mGBA build
with `-DBUILD_LIBRETRO=ON`. With `XP_ADMIN_HOST` unset the admin page works on localhost.
`server/prep.py` drives the emulator directly, without a crowd. Merges to `main` build the image
`ghcr.io/<owner>/<repo>` (amd64 and arm64) with GitHub Actions, so a fork publishes its own.

## Ground rules

- **Bring your own ROM.** No game files are included or ever sent to viewers; only video leaves your
  machine. Only use games you own.
- PlayX is a fan project, not affiliated with Nintendo, Game Freak, The Pokémon Company or X.

## License

[MIT](LICENSE). Made by [@Stormster](https://github.com/Stormster) ·
[@StormsterDev](https://x.com/StormsterDev) on X.
