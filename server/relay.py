"""Pushes the live video to the PlayX relay (Cloudflare Worker + Durable Object), which fans it
out to every viewer. One upload stream from home no matter how many people watch.

Messages to the relay (see relay/src/index.js):
    0x10 + ([u32 len][packet])*   live packets, sent in small batches
    0x11                          the game froze
Packets are batched (default 3 = ~100 ms) because the Workers free plan bills incoming
WebSocket messages at 20:1; 10 messages a second stays well inside it.
"""
import asyncio
import json
import struct

import aiohttp

T_LIVE, T_END = 0x10, 0x11
MAX_QUEUED = 20            # ~2 s of batches; past that the link is too slow, drop and resync


class RelayPublisher:
    def __init__(self, base_url, secret, run_name, encoder, batch=3, on_change=None):
        base = base_url.rstrip("/")
        self.pub_url = f"{base}/pub?run={run_name}"
        self.viewer_url = base.replace("https://", "wss://").replace("http://", "ws://") + f"/ws?run={run_name}"
        self.secret = secret
        self.encoder = encoder
        self.batch = batch
        self.on_change = on_change            # called with (up: bool) on connect / disconnect
        self.up = False
        self.viewers = 0                      # as reported by the relay
        self.sent = 0
        self.dropped = 0
        self._pending = []
        self._queue = asyncio.Queue()
        self._ws = None
        self._task = None

    # called on the event loop for every packet --------------------------------
    def add_packet(self, pkt, is_key):
        if not self.up:
            return
        self._pending.append(pkt)
        if len(self._pending) >= self.batch:
            self._send_pending()

    def end(self):
        """The game froze: send what's left, then the freeze marker."""
        if not self.up:
            return
        self._send_pending()
        self._queue.put_nowait(bytes([T_END]))

    def _send_pending(self):
        if not self._pending:
            return
        msg = bytes([T_LIVE]) + b"".join(struct.pack("<I", len(p)) + p for p in self._pending)
        self._pending = []
        if self._queue.qsize() >= MAX_QUEUED:
            # upload can't keep up: throw the backlog away and restart from a keyframe
            while not self._queue.empty():
                self._queue.get_nowait()
            self.dropped += 1
            self.encoder.force_key()
            return
        self._queue.put_nowait(msg)

    # connection ---------------------------------------------------------------
    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        backoff = 1
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(
                            self.pub_url, heartbeat=20, max_msg_size=4096,
                            headers={"authorization": f"Bearer {self.secret}"}) as ws:
                        self._ws = ws
                        while not self._queue.empty():
                            self._queue.get_nowait()
                        self._pending = []
                        self.encoder.force_key()      # the relay's late joiners need a keyframe
                        self._set_up(True)
                        backoff = 1
                        writer = asyncio.create_task(self._writer(ws))
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        self.viewers = int(json.loads(msg.data).get("viewers", self.viewers))
                                    except (ValueError, TypeError, AttributeError):
                                        pass
                                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                    break
                        finally:
                            writer.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    print("relay:", type(e).__name__, e, flush=True)
                self._ws = None
                self._set_up(False)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _writer(self, ws):
        while True:
            msg = await self._queue.get()
            try:
                await ws.send_bytes(msg)
                self.sent += 1
            except Exception as e:  # noqa: BLE001
                print("relay send failed:", e, flush=True)
                await ws.close()
                return

    def _set_up(self, up):
        if up == self.up:
            return
        self.up = up
        print("relay:", "connected" if up else "down", flush=True)
        if self.on_change:
            self.on_change(up)

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
