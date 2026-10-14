"""PlayX run server.

One process = one run. It owns the emulator (the only thing that touches the ROM),
streams video+audio to viewers, runs the vote room, and autosaves. The game keeps
animating while someone watches, goes quiet after the room has been empty for 30 s,
and wakes as soon as a viewer arrives. The host can always pause it.

    python3 run.py --rom ../roms/firered.gba --core ../core/mgba_libretro.so --data ../data/firered

Env: XP_ADMIN_TOKEN enables /admin endpoints. PORT sets the port (default 8787).
Every setting is listed in docs/configuration.md.
"""
import argparse
import asyncio
import collections
import hashlib
import hmac
import io
import json
import os
import random
import tempfile
import threading
import time
import uuid
import zlib
from html import escape

import numpy as np
from aiohttp import WSMsgType, web
from PIL import Image

from emu import BUTTONS, Emulator
from names import NameFilter
from chunks import ChunkPublisher, LocalSink, PacketEncoder, packet_bytes, sink_from_env
from relay import RelayPublisher

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
VIDEO_EVERY = 2               # send every 2nd frame (~30 fps)
AUDIO_RATE_DIV = 2            # 32768 Hz -> 16384 Hz mono
AUTOSAVE_SECONDS = 60
SAVE_RETRY_SECONDS = 10       # bounded retries, including while paused or empty
KEEP_STATES = 180             # 3 hours of minute-by-minute rollback points
DIRECTIONS = {"up", "down", "left", "right"}
# FireRed's naming keyboard starts repeating after 16 held-frame intervals.
# Release before the 17th held frame; keep an 18-frame direction cycle for walking.
HOLD = {"dir": (16, 2), "btn": (5, 10)}   # (hold frames, release frames) per press
MAX_STEPS = 5                 # biggest "Up x5" style multiplier
ANARCHY_COOLDOWN = 0.25
ANARCHY_QUEUE_CAP = 6
SIMULATED_PLAYER_CAP = 50
SIMULATED_PLAYER_SECONDS = 4   # one input per player per cycle, until the aggregate cap
SIMULATED_INPUT_SECONDS = 0.5  # at most two automatic input attempts per second
SIMULATED_DRIFT_SECONDS = (15, 90)   # random gap between bot joins/leaves
SIMULATED_TARGET_SECONDS = (120, 300) # how long the drift target holds
SIMULATED_GROUP_CHANCE = 0.15         # chance 2-3 bots join or leave together
SIMULATED_TARGET_PULL = 0.7           # chance a move heads toward the target
SIMULATED_BUTTONS = ("up", "down", "left", "right", "a", "a", "b")
PER_IP_VOTE_CAP = 3           # voters per IP counted per window
ANARCHY_BURST_SECONDS = 3
ANARCHY_REST_SECONDS = 2      # a quiet input gap, not a reconnect, ends a burst
ANARCHY_IP_WINDOW = 5
ANARCHY_IP_INPUT_CAP = 36     # three browsers' bounded bursts per five seconds
ANARCHY_LIMIT_TTL = 60
GUARDED = {"start", "select"}  # need 2x the runner-up to win
STATE_PUSH_SECONDS = 0.25
MODE_LOCK_SECONDS = 600       # after a mode switch, it can't switch again for 10 minutes
MODE_HOLD_SECONDS = 10        # a new majority must hold this long before it flips the mode
MODE_THRESHOLD = 0.6          # share of mode voters needed to switch
SNAPSHOT_SECONDS = 10         # in-memory rewind points for the guard
SNAPSHOTS_KEPT = 36           # 6 minutes
GUARD_ROLLBACK_SECONDS = 20   # how far back the guard rewinds
GUARD_CHECK_FRAMES = 30
IDLE_SECONDS = 60             # run clock pauses after this long without a winning input
EMPTY_ROOM_SECONDS = 30       # grace period after the last viewer leaves, before frames/uploads stop

# FireRed (US v1.0, BPRE) RAM layout, from the pokefirered decomp
FR_PARTY_COUNT = 0x02024029
FR_STORAGE_PTR = 0x03005010
FR_SB1_PTR = 0x03005008
FR_SB2_PTR = 0x0300500C
FR_PARTY = 0x02024284
FR_RIVAL_NAME_OFFSET = 0x3A4C   # inside SaveBlock1
BOX_MON_SIZE, BOXES, BOX_SLOTS = 80, 14, 30


def ulaw_encode(x):
    """int16 numpy -> uint8 mu-law."""
    x = x.astype(np.int32)
    sign = (x < 0)
    mag = np.minimum(np.abs(x), 32635) + 0x84
    exp = np.floor(np.log2(mag)).astype(np.int32) - 7
    exp = np.clip(exp, 0, 7)
    mant = (mag >> (exp + 3)) & 0x0F
    out = ~((sign.astype(np.int32) << 7) | (exp << 4) | mant) & 0xFF
    return out.astype(np.uint8)


def atomic_write(path, data):
    """A failed/partial write must never replace the last complete save."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), prefix=".save-", delete=False) as f:
            tmp = f.name
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Emulator thread
# ---------------------------------------------------------------------------
class GameLoop(threading.Thread):
    def __init__(self, emu, data_dir, on_packet):
        super().__init__(daemon=True)
        self.emu = emu
        self.data_dir = data_dir
        self.states_dir = os.path.join(data_dir, "states")
        os.makedirs(self.states_dir, exist_ok=True)
        self.on_packet = on_packet          # called from this thread with (seq, frame, audio_bytes)
        self.steps = collections.deque()    # (button, hold, release)
        self.current = None                 # [button, hold_left, release_left]
        self.viewers = threading.Event()
        self.wake = threading.Event()       # set to end a quiet wait early (vote, viewer, admin)
        self.empty_since = None            # None on a fresh, empty boot; no need to run unseen
        self.empty_room_seconds = EMPTY_ROOM_SECONDS
        self.quiet = False                  # True while frozen (no frames, no video)
        self.on_quiet = None                # called from this thread with "admin" | "empty"
        self.commands = collections.deque() # callables run between frames (rollback etc.)
        self.frame_no = 0
        self.seq = 0
        self.last_save = time.time()
        self.last_save_success = None
        self.last_save_attempt = None
        self.save_error = None
        self.save_failures = 0
        self.next_save_retry = 0.0
        self.running = True
        self.last_input = 0.0               # monotonic time of the last input fed to the game
        self.speed = 1                      # admin fast-forward: 1x-4x
        self.admin_paused = False
        self.idle_seconds = IDLE_SECONDS
        self.snapshots = collections.deque(maxlen=SNAPSHOTS_KEPT)   # (played_frames, blob)
        self.guard_on = True
        self.on_guard = None                # called from this thread with a message
        self._mon_total = None
        self._mon_drop_seen = 0
        self.names = NameFilter(data_dir)
        self.meta_path = os.path.join(data_dir, "meta.json")
        self.meta = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "frames": 0,
                     "guardUndos": 0}
        if os.path.exists(self.meta_path):
            with open(self.meta_path) as f:
                self.meta.update(json.load(f))

    # inputs ----------------------------------------------------------------
    def active(self):
        return time.monotonic() - self.last_input < self.idle_seconds

    def enqueue(self, button, count=1):
        self.last_input = time.monotonic()
        kind = "dir" if button in DIRECTIONS else "btn"
        hold, release = HOLD[kind]
        for _ in range(count):
            self.steps.append((button, hold, release))
        self.wake.set()

    def set_viewers(self, present):
        """Wake on arrival; give the last departure a short reconnection grace period."""
        if present:
            self.viewers.set()
            self.empty_since = None
        else:
            self.empty_since = time.monotonic()
            self.viewers.clear()
        self.wake.set()

    def poke(self):
        """Wake the command loop without overriding host pause or an empty-room freeze."""
        self.wake.set()

    def clock_running(self):
        return self.viewers.is_set() and self.active() and not self.admin_paused and not self.quiet

    def busy_frames(self):
        n = sum(h + r for _, h, r in self.steps)
        if self.current:
            n += self.current[1] + self.current[2]
        return n

    def _apply_input(self):
        if self.current is None and self.steps:
            b, h, r = self.steps.popleft()
            self.current = [b, h, r]
        if self.current is None:
            self.emu.pressed = set()
            return
        b, h, r = self.current
        if h > 0:
            self.emu.pressed = {b}
            self.current[1] -= 1
        elif r > 0:
            self.emu.pressed = set()
            self.current[2] -= 1
        if self.current[1] <= 0 and self.current[2] <= 0:
            self.current = None

    # run clock -------------------------------------------------------------
    def played_seconds(self):
        return self.meta["frames"] / self.emu.fps

    def write_meta(self):
        atomic_write(self.meta_path, json.dumps(self.meta).encode())

    # guard -------------------------------------------------------------------
    def count_mons(self):
        """Pokemon the player owns (party + PC). None if not readable yet."""
        try:
            party = self.emu.read(FR_PARTY_COUNT, 1)[0]
            ptr = self.emu.read_u32(FR_STORAGE_PTR)
            if party > 6 or not (0x02000000 <= ptr < 0x02040000):
                return None
            raw = self.emu.read(ptr + 4, BOXES * BOX_SLOTS * BOX_MON_SIZE)
        except ValueError:
            return None
        flags = np.frombuffer(raw, dtype=np.uint8)[19::BOX_MON_SIZE]
        return party + int(np.count_nonzero(flags & 0x02))

    def check_guard(self):
        total = self.count_mons()
        if total is None:
            return
        if self._mon_total is None or total >= self._mon_total:
            self._mon_total = total
            self._mon_drop_seen = 0
            return
        self._mon_drop_seen += 1
        if self._mon_drop_seen < 2:          # must persist ~1s, ignores mid-move blips
            return
        if not self.guard_on:
            self._mon_total = total
            return
        lost = self._mon_total - total
        target = self.frame_no - GUARD_ROLLBACK_SECONDS * self.emu.fps
        snap = next((b for fr, b in reversed(self.snapshots) if fr <= target), None)
        if snap is None and self.snapshots:
            snap = self.snapshots[0][1]
        if snap is None:
            self._mon_total = total
            return
        self.steps.clear()
        self.current = None
        self.emu.load_state(snap)
        self._mon_total = None
        self._mon_drop_seen = 0
        self.meta["guardUndos"] += 1
        if self.on_guard:
            self.on_guard(f"The crowd released {lost} Pokemon. Undone, rewound {GUARD_ROLLBACK_SECONDS}s.")

    def check_names(self):
        try:
            party = self.emu.read(FR_PARTY_COUNT, 1)[0]
            storage = self.emu.read_u32(FR_STORAGE_PTR)
            sb1 = self.emu.read_u32(FR_SB1_PTR)
            sb2 = self.emu.read_u32(FR_SB2_PTR)
            ok = lambda p: 0x02000000 <= p < 0x02040000
            fixed = self.names.check(
                self.emu, FR_PARTY, party if party <= 6 else 0,
                storage if ok(storage) else 0, sb2 if ok(sb2) else 0,
                sb1 + FR_RIVAL_NAME_OFFSET if ok(sb1) else None)
        except ValueError:
            return
        if fixed and self.on_guard:
            self.on_guard("A name broke the house rules and was replaced.")

    # saves -----------------------------------------------------------------
    def latest_path(self):
        return os.path.join(self.data_dir, "latest.st")

    def autosave(self):
        blob = self.emu.save_state()
        ts = int(time.time())
        atomic_write(os.path.join(self.states_dir, f"{ts}.st"), blob)
        thumb = io.BytesIO()
        Image.fromarray(self.emu.frame_rgb()).save(thumb, "PNG", optimize=True)
        thumb = thumb.getvalue()
        atomic_write(os.path.join(self.states_dir, f"{ts}.png"), thumb)
        atomic_write(self.latest_path(), blob)
        atomic_write(os.path.join(self.data_dir, "game.srm"), self.emu.save_ram())
        files = sorted(f for f in os.listdir(self.states_dir) if f.endswith(".st"))
        for old in files[:-KEEP_STATES]:
            for ext in (".st", ".png"):
                path = os.path.join(self.states_dir, old[:-3] + ext)
                if os.path.exists(path):
                    os.remove(path)
        # one state per hour is kept forever
        hourly = os.path.join(self.data_dir, "hourly")
        os.makedirs(hourly, exist_ok=True)
        hour = time.strftime("%Y%m%d-%H", time.gmtime())
        hp = os.path.join(hourly, f"{hour}.st")
        if not os.path.exists(hp):
            atomic_write(hp, blob)
            atomic_write(hp[:-3] + ".png", thumb)
        self.write_meta()
        self.last_save = time.time()
        self.last_save_success = self.last_save

    def maybe_autosave(self, quiet_entry=False):
        """Contain persistence failures; retries use monotonic time and cannot run per frame."""
        if self.save_error:
            if time.monotonic() < self.next_save_retry:
                return
        elif time.time() - self.last_save <= (5 if quiet_entry else AUTOSAVE_SECONDS):
            return
        self.last_save_attempt = time.time()
        try:
            self.autosave()
        except Exception as e:  # noqa: BLE001 - disk/serialization must not stop emulation
            self.save_error = type(e).__name__
            self.save_failures += 1
            self.next_save_retry = time.monotonic() + SAVE_RETRY_SECONDS
            print(f"autosave failed ({self.save_error}); retry in {SAVE_RETRY_SECONDS}s", flush=True)
        else:
            if self.save_error:
                print("autosave recovered", flush=True)
            self.save_error = None
            self.save_failures = 0
            self.next_save_retry = 0.0

    def persistence_status(self):
        return {"degraded": self.save_error is not None, "error": self.save_error,
                "failures": self.save_failures, "lastAttempt": self.last_save_attempt,
                "lastSaved": self.last_save_success,
                "retryIn": max(0, self.next_save_retry - time.monotonic()) if self.save_error else None}

    def restore_latest(self):
        if os.path.exists(self.latest_path()):
            with open(self.latest_path(), "rb") as f:
                self.emu.load_state(f.read())
            return True
        return False

    # loop ------------------------------------------------------------------
    def quiet_reason(self):
        if self.admin_paused:
            return "admin"
        if not self.viewers.is_set() and (self.empty_since is None
                or time.monotonic() - self.empty_since >= self.empty_room_seconds):
            return "empty"
        return None

    def run(self):
        next_t = time.perf_counter()
        while self.running:
            period = 1.0 / (self.emu.fps * self.speed)
            while self.commands:
                try:
                    self.commands.popleft()()
                except Exception as e:  # noqa: BLE001
                    print("command failed:", e)
            reason = self.quiet_reason()
            if reason:
                if not self.quiet:
                    # Going quiet: no frames means no chunks, so R2 sees zero uploads from here on.
                    self.quiet = True
                    if self.on_quiet:
                        self.on_quiet(reason)
                    self.maybe_autosave(quiet_entry=True)
                elif self.save_error:
                    self.maybe_autosave()      # retry failed quiet saves, then stay quiet
                self.wake.wait(0.25)
                self.wake.clear()
                next_t = time.perf_counter()
                continue
            self.quiet = False
            self._apply_input()
            self.emu.run_frame()
            self.frame_no += 1
            if self.clock_running():
                self.meta["frames"] += 1 / self.speed   # run clock = real play time, even sped up
            if self.frame_no % int(SNAPSHOT_SECONDS * self.emu.fps) == 0:
                self.snapshots.append((self.frame_no, self.emu.save_state()))
            if self.frame_no % GUARD_CHECK_FRAMES == 0:
                self.check_guard()
                # Name filter is parked (names.py); call self.check_names() here to turn it on.
            if self.frame_no % (VIDEO_EVERY * self.speed) == 0:
                self.seq += 1
                if self.speed != 1:                 # fast-forward: keep 30 fps video, drop audio
                    self.emu.drain_audio()
                    self.on_packet(self.seq, self.emu.frame.copy(), b"")
                    continue_audio = False
                else:
                    continue_audio = True
            else:
                continue_audio = False
            if continue_audio:
                stereo = self.emu.drain_audio().reshape(-1, 2).astype(np.int32)
                mono = stereo.mean(axis=1)
                n = (len(mono) // AUDIO_RATE_DIV) * AUDIO_RATE_DIV
                mono = mono[:n].reshape(-1, AUDIO_RATE_DIV).mean(axis=1).astype(np.int16)
                self.on_packet(self.seq, self.emu.frame.copy(), ulaw_encode(mono).tobytes())
            self.maybe_autosave()
            next_t += period
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.25:
                next_t = time.perf_counter()   # fell behind; don't try to catch up


# ---------------------------------------------------------------------------
# Viewers
# ---------------------------------------------------------------------------
class InputBurst:
    """Bound sustained input even across changing keys and duplicate connections."""
    def __init__(self):
        self.started = None
        self.last_request = None
        self.last_input = None
        self.limited = False

    def observe(self, now, unlimited=False):
        if self.last_request is None or now - self.last_request >= ANARCHY_REST_SECONDS:
            self.started = now
            self.limited = False
        self.last_request = now
        self.limited = not unlimited and (self.limited or now - self.started >= ANARCHY_BURST_SECONDS)
        if self.limited:
            return "pause"
        if self.last_input is not None and now - self.last_input < ANARCHY_COOLDOWN:
            return "rate"
        return None

    def accept(self, now):
        self.last_input = now


class Viewer:
    def __init__(self, ws, ip):
        self.ws = ws
        self.ip = ip
        self.id = uuid.uuid4().hex     # replaced by the client's stored id on hello
        self.queue = asyncio.Queue()
        self.video_pending = 0
        self.need_key = True
        self.input_burst = InputBurst()
        self.input_notice_at = 0.0
        self.bytes_sent = 0
        self.fallback = False          # this viewer can't reach the relay and is on R2 chunks


class SimulatedPlayer:
    """An input identity only: never a connection, video subscriber, or mode voter."""
    __slots__ = ("id", "ip", "input_burst", "input_notice_at")

    def __init__(self):
        self.id = uuid.uuid4().hex
        self.ip = "simulated:" + self.id
        self.input_burst = InputBurst()
        self.input_notice_at = 0.0


def valid_simulated_players(value):
    return type(value) is int and 0 <= value <= SIMULATED_PLAYER_CAP


def simulated_range_error(lo, hi):
    """None when lo..hi is a valid bot range, else the message for the host."""
    if not valid_simulated_players(lo) or not valid_simulated_players(hi):
        return f"simulatedMin and simulatedMax must be integers from 0 to {SIMULATED_PLAYER_CAP}"
    if lo > hi:
        return "simulatedMin must not be greater than simulatedMax"
    return None


class Room:
    def __init__(self, game, window_ms):
        self.game = game
        self.viewers = set()
        self.window_ms = window_ms
        self.mode = "democracy"
        self.votes = {}                # voter id -> (button, count, ip)
        self.window_end = time.monotonic() + window_ms / 1000
        self.ticker = collections.deque(maxlen=8)
        self.last_frame = None         # (seq, frame) last broadcast
        self.key_cache = {}
        self.total_inputs = 0
        self.anarchy_by_id = {}       # retained across reconnects; expires after quiet time
        self.anarchy_by_ip = {}       # accepted input timestamps; bounds rotating IDs
        self.anarchy_prune_at = 0.0
        self.publisher = None          # ChunkPublisher when video goes through the CDN
        self.relay = None              # RelayPublisher when the live relay is configured
        self.encoder = PacketEncoder() # one encode per frame, shared by relay and chunks
        self.mode_lock_seconds = MODE_LOCK_SECONDS
        self.theme = "flame"
        self.steps_on = False          # ×2/×3/×5 direction votes (admin toggle, off by default)
        self.anarchy_hold_unlimited = False  # host override; cadence and queue limits still apply
        self.mode_locked_until = 0.0
        self.mode_voters = 0
        self.mode_votes = {}           # connected client id -> (latest preference, first-vote IP)
        self._flip_since = None
        self.simulated_players = []
        self.simulated_next_at = None
        self.simulated_cursor = 0
        self.simulated_min = 0
        self.simulated_max = 0
        self.drift_next_at = None      # when the live bot count next changes
        self.drift_target = 0          # slowly changing count the walk leans toward
        self.drift_target_until = 0.0

    def resize_simulated_players(self, count):
        """Join or leave bots from the end; a leaver's vote and limiter memory go with it."""
        for player in self.simulated_players[count:]:
            self.votes.pop(player.id, None)
            self.anarchy_by_id.pop(player.id, None)
            self.anarchy_by_ip.pop(player.ip, None)
        self.simulated_players = self.simulated_players[:count]
        self.simulated_players.extend(SimulatedPlayer() for _ in range(count - len(self.simulated_players)))

    def set_simulated_range(self, lo, hi):
        error = simulated_range_error(lo, hi)
        if error:
            raise ValueError(error)
        self.simulated_min, self.simulated_max = lo, hi
        count = len(self.simulated_players)
        if not lo <= count <= hi:            # a count already inside the range is kept
            count = random.randint(lo, hi)
        if count != len(self.simulated_players):
            self.resize_simulated_players(count)
            self.simulated_cursor = 0
        self.simulated_next_at = None
        self.drift_next_at = None

    def set_simulated_players(self, count):
        if not valid_simulated_players(count):
            raise ValueError("simulatedPlayers must be an integer from 0 to 50")
        self.set_simulated_range(count, count)

    def drift_simulated_players(self, now):
        """Bounded random walk of the bot count; call only while it should run."""
        lo, hi = self.simulated_min, self.simulated_max
        if lo == hi:
            return
        if self.drift_next_at is None:         # first tick, or resuming: schedule from now
            self.drift_next_at = now + random.uniform(*SIMULATED_DRIFT_SECONDS)
            self.drift_target = random.randint(lo, hi)
            self.drift_target_until = now + random.uniform(*SIMULATED_TARGET_SECONDS)
            return
        if now < self.drift_next_at:
            return
        self.drift_next_at = now + random.uniform(*SIMULATED_DRIFT_SECONDS)
        if now >= self.drift_target_until:
            self.drift_target = random.randint(lo, hi)
            self.drift_target_until = now + random.uniform(*SIMULATED_TARGET_SECONDS)
        count = len(self.simulated_players)
        step = random.randint(2, 3) if random.random() < SIMULATED_GROUP_CHANCE else 1
        if count != self.drift_target and random.random() < SIMULATED_TARGET_PULL:
            sign = 1 if self.drift_target > count else -1
        else:
            sign = random.choice((-1, 1))
        if not lo <= count + sign <= hi:
            sign = -sign
        new = max(lo, min(hi, count + sign * step))
        if new != count:
            self.resize_simulated_players(new)

    def suspend_simulated_players(self):
        self.simulated_next_at = None
        self.drift_next_at = None
        for player in self.simulated_players:
            self.votes.pop(player.id, None)

    def tick_simulated_players(self, now):
        if not self.viewers or self.game.admin_paused:
            self.suspend_simulated_players()
            return
        self.drift_simulated_players(now)
        count = len(self.simulated_players)
        if not count:
            self.simulated_next_at = None
            return
        interval = max(SIMULATED_INPUT_SECONDS, SIMULATED_PLAYER_SECONDS / count)
        if self.simulated_next_at is None:
            self.simulated_next_at = now + interval
        if now < self.simulated_next_at:
            return
        # Schedule from now, never from the missed deadline: no catch-up bursts.
        self.simulated_next_at = now + interval
        player = self.simulated_players[self.simulated_cursor % count]
        self.simulated_cursor = (self.simulated_cursor + 1) % count
        self.handle_vote(player, random.choice(SIMULATED_BUTTONS), 1)

    # video fan-out ---------------------------------------------------------
    def key_bytes(self, seq, frame):
        kb = self.key_cache.get(seq)
        if kb is None:
            kb = zlib.compress(frame.tobytes(), 6)
            self.key_cache = {seq: kb}
        return kb

    def broadcast_packet(self, seq, frame, audio):
        if self.publisher:
            self.last_frame = (seq, frame)
            pkt, is_key = self.encoder.encode(seq, frame, audio)
            if self.relay:
                self.relay.add_packet(pkt, is_key)
            self.publisher.add_packet(pkt, is_key)
            return
        prev = self.last_frame
        self.last_frame = (seq, frame)
        if prev is None:
            delta = None
        else:
            delta = zlib.compress((frame ^ prev[1]).tobytes(), 1)
        for v in self.viewers:
            if v.video_pending >= 4:
                v.need_key = True       # too far behind; skip and resync with a keyframe
                continue
            v.video_pending += 1
            v.queue.put_nowait(("video", seq, frame, audio, delta))

    def chunks_needed(self):
        """R2 chunks only run when the relay can't carry everyone: not configured, down, or some
        viewer can't reach it. Otherwise uploads stay at zero."""
        return (self.relay is None or not self.relay.up
                or any(v.fallback for v in self.viewers))

    def update_video(self):
        if not self.publisher:
            return
        need = self.chunks_needed()
        if need == self.publisher.enabled:
            return
        self.publisher.set_enabled(need)
        print("chunks:", "on" if need else "off (relay carrying everyone)", flush=True)
        if need:
            self.encoder.force_key()          # first chunk can start right away
            if self.game.quiet and self.last_frame is not None:
                # Publish the current picture without advancing a host-paused game.
                seq, frame = self.last_frame
                self.publisher.add_packet(packet_bytes(1, seq, b"", self.key_bytes(seq, frame)), True)
                self.publisher.flush()

    def on_relay_change(self, up):
        self.update_video()
        if up and self.game.quiet and self.last_frame is not None:
            # A new publisher connection needs a picture even if the core stays frozen.
            seq, frame = self.last_frame
            self.relay.add_packet(packet_bytes(1, seq, b"", self.key_bytes(seq, frame)), True)
            self.relay.end()
            self.encoder.force_key()
        self.broadcast_json({"t": "video", "relayUp": up})

    def on_quiet(self, reason):
        """The game just froze. Publish the partial chunk so the CDN has the final picture."""
        if self.publisher:
            self.publisher.flush()
            if self.relay:
                self.relay.end()
            self.encoder.force_key()          # when it wakes, chunks and late joiners start clean

    def send_json(self, v, obj):
        if isinstance(v, SimulatedPlayer):
            return  # Input limits need no transport or accumulating outbound queue.
        v.queue.put_nowait(("json", json.dumps(obj, separators=(",", ":"))))

    def broadcast_json(self, obj):
        s = json.dumps(obj, separators=(",", ":"))
        for v in self.viewers:
            v.queue.put_nowait(("json", s))

    async def sender(self, v):
        while True:
            item = await v.queue.get()
            if item[0] == "json":
                await v.ws.send_str(item[1])
                continue
            _, seq, frame, audio, delta = item
            v.video_pending -= 1
            if v.need_key or delta is None:
                body, kind = self.key_bytes(seq, frame), 1
                v.need_key = False
            else:
                body, kind = delta, 2
            header = bytes([kind]) + len(audio).to_bytes(2, "little") + seq.to_bytes(4, "little")
            msg = header + audio + body
            v.bytes_sent += len(msg)
            await v.ws.send_bytes(msg)

    # voting ----------------------------------------------------------------
    def tally(self):
        counts = collections.Counter()
        per_ip = collections.Counter()
        for _, (b, n, ip) in sorted(self.votes.items(), key=lambda kv: kv[0]):
            if per_ip[ip] >= PER_IP_VOTE_CAP:
                continue
            per_ip[ip] += 1
            counts[f"{b}:{n}"] += 1
        return counts

    def button_totals(self, counts):
        totals = collections.Counter()
        for key, c in counts.items():
            totals[key.split(":")[0]] += c
        return totals

    def close_window(self):
        counts = self.tally()
        self.last_votes = dict(self.votes)
        self.votes = {}
        if not counts:
            return None
        totals = self.button_totals(counts)
        ranked = totals.most_common()
        top_btn, top_n = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0
        if top_btn in GUARDED and top_n < runner * 2:
            top_btn = ranked[1][0]
        ties = [b for b, n in ranked if n == totals[top_btn] and not (b in GUARDED and n < runner * 2)]
        if len(ties) > 1:
            top_btn = random.choice(ties)
        # most popular multiplier for the winning button
        mult = max((k for k in counts if k.startswith(top_btn + ":")), key=lambda k: counts[k])
        n = int(mult.split(":")[1])
        return top_btn, n

    def press(self, button, n, voters=(), human=True):
        self.game.enqueue(button, n)
        if human:                       # the admin counter shows real players' inputs only
            self.total_inputs += 1
        entry = {"b": button, "n": n}
        self.ticker.appendleft(entry)
        # Encode once per outcome, not once per viewer: large rooms get every win.
        base = json.dumps({"t": "win", "b": button, "n": n, "mode": self.mode},
                          separators=(",", ":"))[:-1]
        mine, other = base + ',"you":true}', base + ',"you":false}'
        for v in self.viewers:
            v.queue.put_nowait(("json", mine if v.id in voters else other))

    def handle_vote(self, v, button, n):
        if button not in BUTTONS:
            return
        n = max(1, min(MAX_STEPS, int(n))) if button in DIRECTIONS and self.steps_on else 1
        if self.mode == "anarchy":
            now = time.monotonic()
            # Always observe attempts: rejected spam cannot wait out the rest timer.
            connection_limit = v.input_burst.observe(now, self.anarchy_hold_unlimited)
            if now >= self.anarchy_prune_at:
                self.anarchy_by_id = {identity: burst for identity, burst in self.anarchy_by_id.items()
                                      if now - burst.last_request < ANARCHY_LIMIT_TTL}
                self.anarchy_by_ip = {ip: stamps for ip, stamps in self.anarchy_by_ip.items()
                                      if stamps and now - stamps[-1] < ANARCHY_IP_WINDOW}
                self.anarchy_prune_at = now + ANARCHY_LIMIT_TTL
            stamps = self.anarchy_by_ip.setdefault(v.ip, collections.deque())
            while stamps and now - stamps[0] >= ANARCHY_IP_WINDOW:
                stamps.popleft()
            ip_limit = len(stamps) >= ANARCHY_IP_INPUT_CAP
            queue_full = len(self.game.steps) >= ANARCHY_QUEUE_CAP
            identity_burst = self.anarchy_by_id.get(v.id)
            # Avoid allocating an identity record for every rejected rotated ID.
            if identity_burst is None and not ip_limit and not queue_full and connection_limit is None:
                identity_burst = self.anarchy_by_id[v.id] = InputBurst()
            identity_limit = identity_burst.observe(now, self.anarchy_hold_unlimited) if identity_burst else connection_limit
            if connection_limit == "pause" or identity_limit == "pause" or ip_limit:
                if now >= v.input_notice_at:
                    pause = ANARCHY_REST_SECONDS if not ip_limit else max(
                        ANARCHY_REST_SECONDS, ANARCHY_IP_WINDOW - (now - stamps[0]))
                    self.send_json(v, {"t": "inputlimit", "pauseMs": int(pause * 1000)})
                    v.input_notice_at = now + pause
                return
            if connection_limit or identity_limit:
                return
            if queue_full:
                return
            v.input_burst.accept(now)
            identity_burst.accept(now)
            stamps.append(now)
            self.press(button, 1, voters={v.id}, human=not isinstance(v, SimulatedPlayer))
        else:
            self.votes[v.id] = (button, n, v.ip)

    def set_mode(self, new, lock=True):
        if new == self.mode:
            return
        self.mode = new
        self.votes = {}
        self.window_end = time.monotonic() + self.window_ms / 1000
        if lock:
            self.mode_locked_until = time.monotonic() + self.mode_lock_seconds
        self._flip_since = None
        self.broadcast_json({"t": "modechange", "mode": new,
                             "lockMs": max(0, int((self.mode_locked_until - time.monotonic()) * 1000))})

    def handle_mode(self, v, preference):
        # One choice per browser identity, shared across tabs. Pin its first-vote IP until
        # its last connection leaves, so moving the same ID between IPs cannot move its cap.
        ip = self.mode_votes.get(v.id, (None, v.ip))[1]
        self.mode_votes[v.id] = (preference, ip)

    def forget_mode_voter(self, identity):
        if not any(v.id == identity for v in self.viewers):
            self.mode_votes.pop(identity, None)

    def update_mode(self):
        """Crowd's choice: only viewers who picked a side count; a 60% majority must hold for
        10 s, and after any switch the mode is locked for 10 minutes."""
        active_ids = {v.id for v in self.viewers}
        self.mode_votes = {identity: vote for identity, vote in self.mode_votes.items()
                           if identity in active_ids}
        per_ip = collections.Counter()
        prefs = []
        # Match button-vote ordering/cap; connection count remains independent.
        for _, (preference, ip) in sorted(self.mode_votes.items()):
            if per_ip[ip] < PER_IP_VOTE_CAP:
                per_ip[ip] += 1
                prefs.append(preference)
        self.mode_voters = len(prefs)
        if not prefs:
            self._flip_since = None
            return None
        share = prefs.count("anarchy") / len(prefs)
        want = self.mode
        if share >= MODE_THRESHOLD:
            want = "anarchy"
        elif share <= 1 - MODE_THRESHOLD:
            want = "democracy"
        now = time.monotonic()
        if want == self.mode or now < self.mode_locked_until:
            self._flip_since = None
        elif self._flip_since is None:
            self._flip_since = now
        elif now - self._flip_since >= MODE_HOLD_SECONDS:
            self.set_mode(want)
        return share

    def state_msg(self, share):
        counts = self.tally()
        totals = self.button_totals(counts)
        return {
            "t": "state",
            "mode": self.mode,
            "anarchyHoldUnlimited": self.anarchy_hold_unlimited,
            "anarchyShare": round(share or 0, 3),
            "modeVoters": self.mode_voters,
            "modeLockLeft": max(0, int((self.mode_locked_until - time.monotonic()) * 1000)),
            "modeFlipIn": (max(0, int((MODE_HOLD_SECONDS - (time.monotonic() - self._flip_since)) * 1000))
                           if self._flip_since is not None else None),
            "viewers": len(self.viewers),
            "simulatedPlayers": len(self.simulated_players),
            "windowMs": self.window_ms,
            "left": max(0, int((self.window_end - time.monotonic()) * 1000)),
            "tally": dict(totals),
            "ticker": list(self.ticker),
            "played": round(self.game.played_seconds(), 1),
            "started": self.game.meta["started"],
            "seq": self.last_frame[0] if self.last_frame else 0,
            "paused": not self.game.clock_running(),
            "playing": not self.game.quiet and not self.game.admin_paused,
            "idle": not self.game.active(),
        }

    async def ticker_loop(self):
        while True:
            await asyncio.sleep(STATE_PUSH_SECONDS)
            share = self.update_mode()
            now = time.monotonic()
            self.tick_simulated_players(now)
            if self.mode == "democracy" and now >= self.window_end:
                # don't pile up: wait while a long multi-step input is still running
                if self.game.busy_frames() <= 30:
                    result = self.close_window()
                    if result:
                        backers = {vid for vid, (b, _, _) in self.last_votes.items() if b == result[0]}
                        bots = {p.id for p in self.simulated_players}
                        self.press(*result, voters=backers, human=bool(backers - bots))
                self.window_end = now + self.window_ms / 1000
            if self.viewers:
                # Shared state is encoded once; only each viewer's own vote is appended.
                base = json.dumps(self.state_msg(share), separators=(",", ":"))[:-1]
                for v in self.viewers:
                    mine = self.votes.get(v.id)
                    tail = f',"mine":"{mine[0]}:{mine[1]}"}}' if mine else ',"mine":null}'
                    v.queue.put_nowait(("json", base + tail))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def client_ip(request):
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote or "?")


def make_app(room, game, emu, admin_token, public_origin, settings_path):
    routes = web.RouteTableDef()

    # Asset version = hash of the shipped JS/CSS, so every deploy gets fresh URLs and
    # Cloudflare's cache (which keeps .js/.css for hours) can't serve stale code.
    digest = hashlib.sha1()
    for asset in ("app.js", "styles.css"):
        with open(os.path.join(WEB_DIR, asset), "rb") as f:
            digest.update(f.read())
    asset_version = digest.hexdigest()[:10]

    # The admin token always guards /admin. XP_ADMIN_HOST adds two more locks: admin only
    # answers on those hostnames (comma-separated) and only behind Cloudflare Access (email
    # login); everywhere else /admin and /stats look like 404s. Unset or "*" = token only.
    admin_hosts = [h.strip().lower() for h in os.environ.get("XP_ADMIN_HOST", "").split(",")
                   if h.strip()] or ["*"]

    def host_of(request):
        return (request.headers.get("host") or "").split(":")[0].lower()

    def admin_gate(request):
        if "*" not in admin_hosts:
            if host_of(request) not in admin_hosts or not request.headers.get("cf-access-jwt-assertion"):
                raise web.HTTPNotFound()

    # Old hostnames redirect to new ones (old X posts, bookmarks): XP_REDIRECTS="old=new,...".
    # Only kicks in once XP_ORIGIN points at a new hostname, so it can't redirect to an
    # address that isn't live.
    redirects = {}
    for pair in os.environ.get("XP_REDIRECTS", "").split(","):
        if "=" in pair:
            a, b = (x.strip().lower() for x in pair.split("=", 1))
            redirects[a] = b
    origin_host = (public_origin or "").split("://")[-1].split("/")[0].split(":")[0].lower()
    if origin_host not in redirects.values():
        redirects = {}

    @web.middleware
    async def redirect_old_hosts(request, handler):
        target = redirects.get(host_of(request))
        if target:
            raise web.HTTPMovedPermanently(f"https://{target}{request.path_qs}")
        return await handler(request)

    def admin_ok(request):
        given = request.headers.get("x-admin-token") or request.query.get("k") or ""
        return bool(admin_token) and hmac.compare_digest(given.encode(), admin_token.encode())

    # Who runs this copy: the X account credited on the card, and the link on the handheld.
    x_handle = os.environ.get("XP_X_HANDLE", "").strip().lstrip("@")
    x_site_tag = f'<meta name="twitter:site" content="@{escape(x_handle)}">' if x_handle else ""
    footer_text = escape(os.environ.get("XP_FOOTER_TEXT", "").strip())
    footer_url = escape(os.environ.get("XP_FOOTER_URL", "").strip())

    def page(name):
        async def handler(request):
            with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
                html = f.read()
            origin = public_origin or f"{request.scheme}://{request.host}"
            html = html.replace("__ORIGIN__", origin)
            html = html.replace('<meta name="twitter:site" content="__X_HANDLE__">', x_site_tag)
            html = html.replace("__FOOTER_TEXT__", footer_text).replace("__FOOTER_URL__", footer_url)
            html = html.replace('"/app.js"', f'"/app.js?v={asset_version}"')
            html = html.replace('"/styles.css"', f'"/styles.css?v={asset_version}"')
            return web.Response(text=html, content_type="text/html",
                                headers={"cache-control": "no-store"})
        return handler

    routes.get("/")(page("index.html"))
    routes.get("/embed")(page("embed.html"))
    routes.get("/play")(page("play.html"))

    @routes.get("/ws")
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
        await ws.prepare(request)
        v = Viewer(ws, client_ip(request))
        room.viewers.add(v)
        game.set_viewers(True)
        sender = asyncio.create_task(room.sender(v))
        room.send_json(v, {"t": "hello", "w": 240, "h": 160,
                           "audioRate": int(emu.sample_rate / AUDIO_RATE_DIV),
                           "maxSteps": MAX_STEPS, "steps": room.steps_on,
                           "anarchyHoldUnlimited": room.anarchy_hold_unlimited,
                           "viewers": len(room.viewers), "simulatedPlayers": len(room.simulated_players),
                           "video": "chunks" if room.publisher else "ws",
                           "relay": room.relay.viewer_url if room.relay else None,
                           "relayUp": bool(room.relay and room.relay.up),
                           "theme": room.theme})
        if room.publisher and room.publisher.latest:
            n, url, end = room.publisher.latest
            room.send_json(v, {"t": "chunk", "n": n, "url": url, "end": end})
        if room.last_frame:
            v.need_key = True
        if not room.publisher:
            # A frozen game has no new packets to send; give joiners its current picture.
            seq, frame = room.last_frame or (game.seq, emu.frame.copy())
            v.video_pending += 1
            v.queue.put_nowait(("video", seq, frame, b"", None))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                t = data.get("t")
                if t == "hello" and isinstance(data.get("id"), str) and 8 <= len(data["id"]) <= 64:
                    previous_id = v.id
                    v.id = data["id"]
                    room.forget_mode_voter(previous_id)
                elif t == "vote":
                    room.handle_vote(v, str(data.get("b", "")), data.get("n", 1))
                elif t == "mode" and data.get("m") in ("democracy", "anarchy"):
                    room.handle_mode(v, data["m"])
                elif t in ("fallback", "relayok"):
                    v.fallback = t == "fallback"
                    room.update_video()
        finally:
            room.viewers.discard(v)
            room.forget_mode_voter(v.id)
            sender.cancel()
            room.update_video()
            if not room.viewers:
                room.suspend_simulated_players()
                game.set_viewers(False)
        return ws

    @routes.get("/latest.png")
    async def latest_png(request):
        frame = room.last_frame[1] if room.last_frame else emu.frame
        f = frame.astype(np.uint32)
        rgb = np.stack([((f >> 11) & 31) * 255 // 31, ((f >> 5) & 63) * 255 // 63, (f & 31) * 255 // 31], -1)
        img = Image.fromarray(rgb.astype(np.uint8)).resize((960, 640), Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return web.Response(body=buf.getvalue(), content_type="image/png",
                            headers={"cache-control": "public, max-age=30"})

    @routes.get("/healthz")
    async def healthz(request):
        healthy = game.running and game.is_alive()
        return web.Response(text="ok" if healthy else "unavailable", status=200 if healthy else 503,
                            headers={"cache-control": "no-store"})

    @routes.get("/stats")
    async def stats(request):
        admin_gate(request)
        if not admin_ok(request):
            raise web.HTTPForbidden(text="bad admin token")
        return web.json_response({
            "viewers": len(room.viewers), "mode": room.mode, "frame": game.frame_no,
            "simulatedPlayers": len(room.simulated_players),
            "inputs": room.total_inputs, "paused": bool(game.quiet or game.admin_paused),
            "played": game.played_seconds(), "started": game.meta["started"],
            "guardUndos": game.meta["guardUndos"],
            "bytesSent": [v.bytes_sent for v in room.viewers],
            "quiet": game.quiet,
            "chunks": ({"on": room.publisher.enabled,
                        "uploads": getattr(room.publisher.sink, "uploads", None),
                        "deletes": getattr(room.publisher.sink, "deletes", None),
                        "inBucket": len(room.publisher.live)} if room.publisher else None),
            "relay": ({"up": room.relay.up, "viewers": room.relay.viewers, "sent": room.relay.sent,
                       "dropped": room.relay.dropped} if room.relay else None),
            "fallbackViewers": sum(1 for v in room.viewers if v.fallback),
        })

    # ---------------------------------------------------------------- admin
    THEMES = ("flame", "cobalt", "graphite", "pearl", "lime")

    def require_admin(request):
        admin_gate(request)
        if not admin_ok(request):
            raise web.HTTPForbidden(text="bad admin token")

    def current_settings():
        return {"window": room.window_ms, "idle": game.idle_seconds,
                "emptyRoom": game.empty_room_seconds, "guard": game.guard_on,
                "modeLock": room.mode_lock_seconds, "theme": room.theme, "speed": game.speed,
                "paused": game.admin_paused, "steps": room.steps_on,
                "simulatedMin": room.simulated_min, "simulatedMax": room.simulated_max,
                "simulatedPlayers": len(room.simulated_players),   # live count
                "anarchyHoldUnlimited": room.anarchy_hold_unlimited}

    def save_settings():
        tmp = settings_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current_settings(), f)
        os.replace(tmp, settings_path)

    @routes.get("/admin")
    async def admin_page(request):
        admin_gate(request)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html"), encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html",
                            headers={"cache-control": "no-store", "x-robots-tag": "noindex"})

    @routes.get("/admin/api/status")
    async def admin_status(request):
        require_admin(request)
        lock_left = max(0, int(room.mode_locked_until - time.monotonic()))
        return web.json_response({
            "settings": current_settings(),
            "viewers": len(room.viewers), "mode": room.mode, "modeLockLeft": lock_left,
            "simulatedPlayers": len(room.simulated_players),
            "modeVoters": room.mode_voters, "inputs": room.total_inputs,
            "played": game.played_seconds(), "started": game.meta["started"],
            "guardUndos": game.meta["guardUndos"], "idle": not game.active(),
            "videoPaused": bool(game.quiet or game.admin_paused), "quiet": game.quiet,
            "gameAlive": game.running and game.is_alive(), "persistence": game.persistence_status(),
            "relay": {"up": room.relay.up, "viewers": room.relay.viewers} if room.relay else None,
            "chunksOn": bool(room.publisher and room.publisher.enabled),
            "fallbackViewers": sum(1 for v in room.viewers if v.fallback),
        })

    @routes.post("/admin/api/config")
    async def admin_config(request):
        require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="config must be an object")
        sim_range = None
        if "simulatedPlayers" in body and ("simulatedMin" in body or "simulatedMax" in body):
            raise web.HTTPBadRequest(text="send simulatedMin/simulatedMax or the legacy simulatedPlayers, not both")
        if "simulatedPlayers" in body:
            if not valid_simulated_players(body["simulatedPlayers"]):
                raise web.HTTPBadRequest(text=f"simulatedPlayers must be an integer from 0 to {SIMULATED_PLAYER_CAP}")
            sim_range = (body["simulatedPlayers"],) * 2
        elif "simulatedMin" in body or "simulatedMax" in body:
            sim_range = (body.get("simulatedMin", room.simulated_min), body.get("simulatedMax", room.simulated_max))
            error = simulated_range_error(*sim_range)
            if error:
                raise web.HTTPBadRequest(text=error)
        if "anarchyHoldUnlimited" in body and type(body["anarchyHoldUnlimited"]) is not bool:
            raise web.HTTPBadRequest(text="anarchyHoldUnlimited must be a boolean")
        if "window" in body:
            room.window_ms = max(500, min(10000, int(body["window"])))
        if "idle" in body:
            game.idle_seconds = max(10, min(3600, int(body["idle"])))
        if "emptyRoom" in body:
            game.empty_room_seconds = max(5, min(600, int(body["emptyRoom"])))
        if "guard" in body:
            game.guard_on = bool(body["guard"])
        if "steps" in body and bool(body["steps"]) != room.steps_on:
            room.steps_on = bool(body["steps"])
            room.votes = {}                    # drop any multiplied votes already cast this window
            room.broadcast_json({"t": "steps", "on": room.steps_on})
        if "anarchyHoldUnlimited" in body and body["anarchyHoldUnlimited"] != room.anarchy_hold_unlimited:
            room.anarchy_hold_unlimited = body["anarchyHoldUnlimited"]
            room.broadcast_json({"t": "holdsetting", "anarchyHoldUnlimited": room.anarchy_hold_unlimited})
        if "modeLock" in body:
            duration = max(0, min(7200, int(body["modeLock"])))
            if duration != room.mode_lock_seconds:
                room.mode_lock_seconds = duration
                room.mode_locked_until = 0.0
                room._flip_since = None
                room.broadcast_json({"t": "modelock", "lockMs": 0})
        if body.get("mode") in ("democracy", "anarchy"):
            room.set_mode(body["mode"])
        if body.get("theme") in THEMES and body["theme"] != room.theme:
            room.theme = body["theme"]
            room.broadcast_json({"t": "theme", "id": room.theme})
        if "speed" in body and int(body["speed"]) in (1, 2, 3, 4) and int(body["speed"]) != game.speed:
            game.speed = int(body["speed"])
            room.broadcast_json({"t": "notice", "text": f"Fast-forward: {game.speed}x" if game.speed > 1
                                 else "Back to normal speed."})
        game.wake.set()
        if "paused" in body and bool(body["paused"]) != game.admin_paused:
            game.admin_paused = bool(body["paused"])
            if game.admin_paused:
                room.suspend_simulated_players()
            room.broadcast_json({"t": "notice", "text": "Paused by the host." if game.admin_paused
                                 else "Back on!"})
        if sim_range:
            room.set_simulated_range(*sim_range)
        save_settings()
        return web.json_response(current_settings())

    def list_saves():
        out = []
        for folder, kind in ((game.states_dir, "minute"), (os.path.join(game.data_dir, "hourly"), "hourly")):
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.endswith(".st"):
                    path = os.path.join(folder, name)
                    out.append({"id": f"{kind}/{name[:-3]}", "kind": kind,
                                "time": int(os.path.getmtime(path)),
                                "thumb": os.path.exists(path[:-3] + ".png")})
        return sorted(out, key=lambda x: x["time"], reverse=True)

    def save_path(save_id, ext):
        kind, _, name = save_id.partition("/")
        folder = {"minute": game.states_dir, "hourly": os.path.join(game.data_dir, "hourly")}.get(kind)
        name = os.path.basename(name)
        if not folder or not name:
            raise web.HTTPNotFound()
        path = os.path.join(folder, name + ext)
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return path

    @routes.get("/admin/api/saves")
    async def admin_saves(request):
        require_admin(request)
        return web.json_response(list_saves()[:200])

    @routes.get("/admin/api/thumb/{kind}/{name}")
    async def admin_thumb(request):
        require_admin(request)
        path = save_path(f"{request.match_info['kind']}/{request.match_info['name']}", ".png")
        return web.FileResponse(path, headers={"cache-control": "private, max-age=86400"})

    @routes.post("/admin/api/rollback")
    async def admin_rollback(request):
        require_admin(request)
        body = await request.json()
        path = save_path(str(body.get("id", "")), ".st")
        with open(path, "rb") as f:
            blob = f.read()
        game.steps.clear()

        def do_rollback():
            emu.load_state(blob)
            game._mon_total = None
            game.current = None
        game.commands.append(do_rollback)
        game.poke()                          # apply the rollback even while the core is frozen
        room.broadcast_json({"t": "notice", "text": "The host rolled the game back to an earlier save."})
        return web.json_response({"ok": True, "id": body.get("id")})

    app = web.Application(middlewares=[redirect_old_hosts])
    app.add_routes(routes)
    if room.publisher and isinstance(room.publisher.sink, LocalSink):
        cdn_dir = os.path.abspath(room.publisher.sink.dir)

        async def cdn(request):
            path = os.path.normpath(os.path.join(cdn_dir, request.match_info["key"]))
            if not path.startswith(cdn_dir + os.sep) or not os.path.isfile(path):
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers={"cache-control": "public, max-age=86400, immutable",
                                                   "access-control-allow-origin": "*"})
        app.router.add_get("/cdn/{key:.+}", cdn)
    app.router.add_static("/", WEB_DIR, show_index=False)
    return app


def load_settings(room, game, settings_path):
    """Restore host settings; invalid or legacy simulated counts stay off."""
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                saved = json.load(f)
            room.window_ms = int(saved.get("window", room.window_ms))
            game.idle_seconds = int(saved.get("idle", game.idle_seconds))
            game.empty_room_seconds = max(5, min(600, int(saved.get("emptyRoom", EMPTY_ROOM_SECONDS))))
            game.guard_on = bool(saved.get("guard", game.guard_on))
            room.mode_lock_seconds = int(saved.get("modeLock", room.mode_lock_seconds))
            room.theme = saved.get("theme", room.theme)
            room.steps_on = bool(saved.get("steps", False))
            room.anarchy_hold_unlimited = saved.get("anarchyHoldUnlimited", False) is True
            game.speed = int(saved.get("speed", 1)) if int(saved.get("speed", 1)) in (1, 2, 3, 4) else 1
            game.admin_paused = bool(saved.get("paused", False))
            if "simulatedMin" in saved or "simulatedMax" in saved:
                lo, hi = saved.get("simulatedMin", 0), saved.get("simulatedMax", 0)
            else:                              # legacy fixed count
                lo = hi = saved.get("simulatedPlayers", 0)
            lo, hi = (lo, hi) if simulated_range_error(lo, hi) is None else (0, 0)
            live = saved.get("simulatedPlayers")
            if valid_simulated_players(live):  # resume the crowd a restart interrupted
                room.resize_simulated_players(max(lo, min(hi, live)))
            room.set_simulated_range(lo, hi)
            print("settings loaded:", saved, flush=True)
        except (ValueError, OSError) as e:
            print("settings.json unreadable, using defaults:", e, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rom", required=True)
    ap.add_argument("--core", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--window", type=int, default=1500)
    ap.add_argument("--origin", default=os.environ.get("XP_ORIGIN", ""))
    args = ap.parse_args()

    emu = Emulator(args.core, args.rom, args.data)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    room_holder = {}

    def on_packet(seq, frame, audio):
        loop.call_soon_threadsafe(room_holder["room"].broadcast_packet, seq, frame, audio)

    game = GameLoop(emu, args.data, on_packet)
    srm = os.path.join(args.data, "game.srm")
    if not game.restore_latest() and os.path.exists(srm):
        with open(srm, "rb") as f:
            emu.load_save_ram(f.read())
    room = Room(game, args.window)
    room_holder["room"] = room
    if os.environ.get("XP_VIDEO", "chunks") == "chunks":
        sink = sink_from_env(args.data)
        run_name = os.environ.get("XP_RUN_NAME", "firered")
        room.publisher = ChunkPublisher(
            sink, run_name,
            lambda n, url, end: room.broadcast_json({"t": "chunk", "n": n, "url": url, "end": end}),
            ttl=int(os.environ.get("XP_CHUNK_TTL", 300)))
        print("video: chunks ->", type(sink).__name__, flush=True)
        if os.environ.get("XP_RELAY_URL"):
            if not os.environ.get("XP_RELAY_SECRET"):
                raise SystemExit("XP_RELAY_URL is set but XP_RELAY_SECRET is missing")
            room.relay = RelayPublisher(os.environ["XP_RELAY_URL"], os.environ["XP_RELAY_SECRET"],
                                        run_name, room.encoder, on_change=room.on_relay_change)
            print("video: live relay ->", room.relay.pub_url, flush=True)
    game.on_guard = lambda text: loop.call_soon_threadsafe(
        room.broadcast_json, {"t": "notice", "text": text})
    # same queue as video packets, so the flush always lands after the last frame
    game.on_quiet = lambda reason: loop.call_soon_threadsafe(room.on_quiet, reason)
    game.start()

    settings_path = os.path.join(args.data, "settings.json")
    load_settings(room, game, settings_path)
    app = make_app(room, game, emu, os.environ.get("XP_ADMIN_TOKEN"), args.origin, settings_path)

    async def start_bg(app_):
        app_["ticker"] = asyncio.create_task(room.ticker_loop())
        if room.relay:
            room.relay.start()

    app.on_startup.append(start_bg)

    async def save_on_exit(app_):
        # docker stop / restart / auto-update: stop the game loop, then save from this thread
        game.running = False
        game.viewers.set()                       # wake the loop if it was paused
        game.wake.set()
        await asyncio.to_thread(game.join, 5)
        try:
            game.autosave()
            print("saved on shutdown", flush=True)
        except Exception as e:  # noqa: BLE001
            print("save on shutdown failed:", e, flush=True)
        # let viewers go right away (they auto-reconnect) instead of holding the exit open
        for v in list(room.viewers):
            try:
                await v.ws.close(code=1012, message=b"restarting")
            except Exception:  # noqa: BLE001
                pass
        if room.relay:
            await room.relay.close()
        if room.publisher:
            await room.publisher.close()         # deletes this boot's chunks (free), then closes

    app.on_shutdown.append(save_on_exit)
    web.run_app(app, port=int(os.environ.get("PORT", 8787)), loop=loop, print=lambda *_: None,
                shutdown_timeout=3)


if __name__ == "__main__":
    main()
