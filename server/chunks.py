"""Video chunks for CDN delivery.

The stream is cut into ~2 second immutable chunks. Each chunk starts with a keyframe,
so a viewer can start from any chunk. Chunks go to Cloudflare R2 (S3 API) and viewers
fetch them through Cloudflare's cache, so the video never leaves your house more than once.

Chunk file layout (little endian):
    b"XPC1" | u32 chunk_no | u16 packet_count | packets...
    packet  = u32 length | [kind u8][audioLen u16][seq u32][mu-law audio][zlib video]
(kind 1 = keyframe RGB565, kind 2 = XOR delta against the previous packet's frame)

Packets are encoded once (PacketEncoder) and shared by the live relay and the chunk path.
A keyframe goes out every 60 packets, and chunk boundaries follow keyframes.
"""
import asyncio
import collections
import datetime as dt
import hashlib
import hmac
import os
import struct
import time
import zlib
from urllib.parse import quote

import aiohttp

MAGIC = b"XPC1"
PACKETS_PER_CHUNK = 60          # 60 packets at ~29.9 fps = ~2.0 s
CHUNK_TTL_SECONDS = 300         # chunks are deleted this long after upload (deletes are free on R2)
CLEANUP_EVERY_SECONDS = 30


def packet_bytes(kind, seq, audio, video):
    return bytes([kind]) + len(audio).to_bytes(2, "little") + seq.to_bytes(4, "little") + audio + video


class PacketEncoder:
    """Turns frames into packets: a keyframe every `gop` packets (or on demand), XOR deltas between."""

    def __init__(self, gop=PACKETS_PER_CHUNK):
        self.gop = gop
        self.prev = None
        self.since_key = 0
        self._force = True

    def force_key(self):
        """Next packet is a keyframe (relay reconnect, chunks switched on, rollback)."""
        self._force = True

    def encode(self, seq, frame, audio):
        """Returns (packet_bytes, is_key)."""
        key = self._force or self.prev is None or self.since_key >= self.gop
        if key:
            body, kind = zlib.compress(frame.tobytes(), 6), 1
            self._force = False
            self.since_key = 0
        else:
            body, kind = zlib.compress((frame ^ self.prev).tobytes(), 1), 2
        self.since_key += 1
        self.prev = frame
        return packet_bytes(kind, seq, audio, body), key


class ChunkBuilder:
    """Groups packets into chunks. Every chunk starts with a keyframe: leading deltas are dropped,
    and a new keyframe closes the chunk before it."""

    def __init__(self, per_chunk=PACKETS_PER_CHUNK):
        self.per_chunk = per_chunk
        self.packets = []
        self.number = 0

    def add_packet(self, pkt, is_key):
        """Returns a list of finished (chunk_no, bytes); usually empty."""
        out = []
        if is_key and self.packets:
            out.append(self._finish())
        if not self.packets and not is_key:
            return out                      # wait for a keyframe
        self.packets.append(pkt)
        if len(self.packets) >= self.per_chunk:
            out.append(self._finish())
        return out

    def add(self, seq, frame, audio, encoder=None):
        """Convenience for tests: encode and add."""
        encoder = encoder or getattr(self, "_enc", None) or PacketEncoder(self.per_chunk)
        self._enc = encoder
        done = self.add_packet(*encoder.encode(seq, frame, audio))
        return done[-1] if done else None

    def flush(self):
        """Close the current chunk early (the stream is about to pause). None if it's empty."""
        return self._finish() if self.packets else None

    def drop(self):
        self.packets = []

    def _finish(self):
        out = [MAGIC, struct.pack("<IH", self.number, len(self.packets))]
        for p in self.packets:
            out.append(struct.pack("<I", len(p)))
            out.append(p)
        done = (self.number, b"".join(out))
        self.number += 1
        self.packets = []
        return done


class LocalSink:
    """Writes chunks to a folder that the run server also serves at /cdn (testing / LAN)."""

    def __init__(self, directory, public_base="/cdn"):
        self.dir = directory
        self.public_base = public_base.rstrip("/")
        os.makedirs(directory, exist_ok=True)

    async def put(self, key, data):
        path = os.path.join(self.dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await asyncio.to_thread(self._write, path, data)

    @staticmethod
    def _write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    async def delete(self, key):
        try:
            await asyncio.to_thread(os.remove, os.path.join(self.dir, key))
        except FileNotFoundError:
            pass

    def url(self, key):
        return f"{self.public_base}/{key}"

    async def close(self):
        pass


def sigv4_headers(method, host, path, payload, access_key, secret_key, region="auto", service="s3",
                  extra=None, now=None):
    """AWS Signature V4 headers for a single S3 request (R2 uses region 'auto')."""
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    for k, v in (extra or {}).items():
        headers[k.lower()] = v
    signed = sorted(headers)
    canonical_headers = "".join(f"{k}:{' '.join(str(headers[k]).split())}\n" for k in signed)
    signed_headers = ";".join(signed)
    canonical_request = "\n".join([method, quote(path, safe="/-_.~"), "", canonical_headers,
                                   signed_headers, payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical_request.encode()).hexdigest()])

    def h(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = h(h(h(h(("AWS4" + secret_key).encode(), date), region), service), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k_: v for k_, v in headers.items() if k_ != "host"}
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed_headers}, Signature={signature}")
    return out


class R2Sink:
    """Uploads chunks to an R2 bucket through the S3-compatible API."""

    def __init__(self, account_id, bucket, access_key, secret_key, public_base):
        self.host = f"{account_id}.r2.cloudflarestorage.com"
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.public_base = public_base.rstrip("/")
        self.session = None
        self.uploads = 0
        self.failures = 0
        self.deletes = 0

    def _session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self.session

    async def put(self, key, data):
        self._session()
        path = f"/{self.bucket}/{key}"
        extra = {"content-type": "application/octet-stream",
                 "cache-control": "public, max-age=86400, immutable"}
        for attempt in range(3):
            headers = sigv4_headers("PUT", self.host, path, data, self.access_key, self.secret_key,
                                    extra=extra)
            try:
                async with self.session.put(f"https://{self.host}{quote(path, safe='/-_.~')}",
                                            data=data, headers=headers) as r:
                    if r.status == 200:
                        self.uploads += 1
                        return
                    body = (await r.text())[:300]
                    print(f"R2 upload {key} failed: HTTP {r.status} {body}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"R2 upload {key} error: {e}")
            await asyncio.sleep(0.3 * (attempt + 1))
        self.failures += 1
        raise RuntimeError(f"upload failed for {key}")

    async def delete(self, key):
        """DeleteObject. Free on R2 (not a Class A or B operation)."""
        path = f"/{self.bucket}/{key}"
        headers = sigv4_headers("DELETE", self.host, path, b"", self.access_key, self.secret_key)
        async with self._session().delete(f"https://{self.host}{quote(path, safe='/-_.~')}",
                                          headers=headers) as r:
            if r.status not in (200, 204, 404):
                raise RuntimeError(f"delete {key}: HTTP {r.status} {(await r.text())[:200]}")
        self.deletes += 1

    def url(self, key):
        return f"{self.public_base}/{key}"

    async def close(self):
        if self.session:
            await self.session.close()


def sink_from_env(data_dir):
    """R2 if configured, otherwise a local folder served at /cdn."""
    env = os.environ
    if env.get("XP_R2_BUCKET"):
        need = ["XP_R2_ACCOUNT_ID", "XP_R2_ACCESS_KEY_ID", "XP_R2_SECRET_ACCESS_KEY", "XP_R2_PUBLIC_BASE"]
        missing = [k for k in need if not env.get(k)]
        if missing:
            raise SystemExit(f"R2 is enabled but missing: {', '.join(missing)}")
        return R2Sink(env["XP_R2_ACCOUNT_ID"], env["XP_R2_BUCKET"], env["XP_R2_ACCESS_KEY_ID"],
                      env["XP_R2_SECRET_ACCESS_KEY"], env["XP_R2_PUBLIC_BASE"])
    return LocalSink(os.path.join(data_dir, "cdn"))


class ChunkPublisher:
    """Builds chunks from packets, uploads them in order, reports which are ready, and deletes
    them again once nobody can still need them."""

    def __init__(self, sink, run_name, on_ready, ttl=CHUNK_TTL_SECONDS):
        self.sink = sink
        self.builder = ChunkBuilder()
        self.boot = str(int(time.time()))     # new folder per boot: cached chunk names never collide
        self.prefix = f"{run_name}/{self.boot}"
        self.on_ready = on_ready              # called with (chunk_no, url, end)
        self.latest = None                    # (chunk_no, url, end) of the newest uploaded chunk
        self.ttl = ttl
        self.live = collections.deque()       # (uploaded_at, key) still in the bucket, oldest first
        self._done = {}
        self._next_announce = 0
        self._cleaner = None
        self.enabled = True                   # off while every viewer gets video from the relay

    def set_enabled(self, on):
        if on == self.enabled:
            return
        self.enabled = on
        if not on:
            self.builder.drop()

    def add_packet(self, pkt, is_key):
        if not self.enabled:
            return
        for done in self.builder.add_packet(pkt, is_key):
            self._start(*done, end=False)

    def add(self, seq, frame, audio):
        """Encode-and-add, for callers that don't share an encoder (tests)."""
        if not hasattr(self, "_enc"):
            self._enc = PacketEncoder(self.builder.per_chunk)
        self.add_packet(*self._enc.encode(seq, frame, audio))

    def flush(self):
        """The game is going quiet: publish what we have so the last picture is on the CDN.
        The chunk is marked end=True so players know the gap that follows is a pause, not lag."""
        if not self.enabled:
            return
        done = self.builder.flush()
        if done:
            self._start(*done, end=True)
        elif self.latest and not self.latest[2]:
            # chunk boundary landed exactly on the pause: re-announce the newest one as the end
            n, url, _ = self.latest
            self.latest = (n, url, True)
            self.on_ready(n, url, True)

    def _start(self, n, data, end):
        loop = asyncio.get_running_loop()
        loop.create_task(self._upload(n, data, end))
        if self._cleaner is None and self.ttl:
            self._cleaner = loop.create_task(self._cleanup_loop())

    def _key(self, n):
        return f"{self.prefix}/{n}.bin"

    async def _upload(self, n, data, end):
        key = self._key(n)
        ok = True
        try:
            await self.sink.put(key, data)
            self.live.append((time.monotonic(), key))
        except Exception as e:  # noqa: BLE001
            print("chunk", n, "dropped:", e)
            ok = False
        self._done[n] = (ok, end)
        # announce strictly in order; never announce a chunk that failed to upload
        while self._next_announce in self._done:
            m = self._next_announce
            good, is_end = self._done.pop(m)
            self._next_announce += 1
            if not good:
                continue
            url = self.sink.url(self._key(m))
            self.latest = (m, url, is_end)
            self.on_ready(m, url, is_end)

    # cleanup -----------------------------------------------------------------
    def expired(self, now=None):
        """Keys old enough to delete. The newest chunk is always kept: late joiners start there."""
        now = time.monotonic() if now is None else now
        keep = self._key(self.latest[0]) if self.latest else None
        out = []
        while self.live and now - self.live[0][0] >= self.ttl and self.live[0][1] != keep:
            out.append(self.live.popleft()[1])
        return out

    async def _delete_many(self, keys, parallel=8):
        sem = asyncio.Semaphore(parallel)

        async def one(key):
            async with sem:
                try:
                    await self.sink.delete(key)
                except Exception as e:  # noqa: BLE001
                    print("chunk delete failed (bucket lifecycle rule will catch it):", key, e)
        await asyncio.gather(*(one(k) for k in keys))

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(CLEANUP_EVERY_SECONDS)
            keys = self.expired()
            if keys:
                await self._delete_many(keys)

    async def close(self, timeout=4.0):
        """Shutdown: this boot's chunks are useless after a restart (new folder), so delete them.
        Best effort; anything left is caught by the bucket's 1-day lifecycle rule."""
        if self._cleaner:
            self._cleaner.cancel()
        keys = [k for _, k in self.live]
        self.live.clear()
        if keys:
            try:
                await asyncio.wait_for(self._delete_many(keys), timeout)
            except asyncio.TimeoutError:
                print("shutdown: some chunks left for the lifecycle rule")
        await self.sink.close()
