#!/bin/sh
# PlayX first-time setup. Safe to run again: it never overwrites an existing .env or ROM.
#   ./setup.sh path/to/your-game.gba
set -eu

say() { printf '%s\n' "$*"; }

rom=""
if [ "${1:-}" ]; then
  [ -f "$1" ] || { say "No file at $1"; exit 1; }
  rom=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
fi
cd "$(dirname "$0")"
mkdir -p roms data

# 1. The ROM (yours; it is never committed or sent to viewers)
if [ -d roms/game.gba ]; then
  # docker compose up without a ROM leaves an empty folder where the file should be
  say "roms/game.gba is a folder (Docker made it when the ROM was missing). Remove it:"
  say "  sudo rmdir roms/game.gba    then run this again."
  exit 1
fi
if [ "$rom" ]; then
  if [ -f roms/game.gba ]; then
    say "roms/game.gba already exists; leaving it. Delete it first to swap games."
  else
    cp "$rom" roms/game.gba
    say "Copied your ROM to roms/game.gba"
  fi
fi

# 2. Settings, with a fresh admin token and the right owner for ./data
if [ -f .env ]; then
  say ".env already exists; leaving it."
else
  token=$(openssl rand -hex 24 2>/dev/null || od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
  uid=$(id -u); gid=$(id -g)
  if [ "$uid" = 0 ]; then uid=99; gid=100; fi     # running as root: the image's built-in user
  sed -e "s|^XP_ADMIN_TOKEN=.*|XP_ADMIN_TOKEN=$token|" \
      -e "s|^PUID=.*|PUID=$uid|" -e "s|^PGID=.*|PGID=$gid|" .env.example > .env
  chmod 600 .env
  # under sudo, keep .env readable by the person who ran it
  if [ "$(id -u)" = 0 ] && [ "${SUDO_UID:-}" ]; then chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" .env; fi
  say "Wrote .env. Your admin token (also in .env): $token"
fi
if [ "$(id -u)" = 0 ]; then
  chown -R "$(sed -n 's/^PUID=//p' .env):$(sed -n 's/^PGID=//p' .env)" data
fi

if [ ! -f roms/game.gba ]; then
  say ""
  say "Last step: run ./setup.sh path/to/your-game.gba (or copy your ROM to roms/game.gba)."
  exit 1
fi
chmod 644 roms/game.gba

# 3. Docker
if ! docker compose version >/dev/null 2>&1; then
  say "Docker with the compose plugin is needed: https://docs.docker.com/get-docker/"
  exit 1
fi

say ""
say "Ready. Start it with:   docker compose up -d"
say "Then open:              http://localhost:8787   (admin: http://localhost:8787/admin)"
say "Going public:           docs/deploy.md"
