"""Video lifecycle checks using disposable storage and a fake core."""
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from chunks import ChunkBuilder, ChunkPublisher, LocalSink, PacketEncoder
from run import GameLoop, Room, make_app


class FakeCore:
    fps = 60
    sample_rate = 32768
    frame = np.zeros((160, 240), dtype=np.uint16)
    pressed = set()

    def run_frame(self):
        pass

    def drain_audio(self):
        return np.zeros(512, dtype=np.int16)

    def read(self, *args):
        raise ValueError('no game memory in fake core')


class VideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_viewer_connect_freeze_rejoin_publishes_and_wakes_without_votes(self):
        with tempfile.TemporaryDirectory() as data:
            loop = asyncio.get_running_loop()
            room = None
            game = GameLoop(FakeCore(), data, lambda *args: loop.call_soon_threadsafe(room.broadcast_packet, *args))
            game.empty_room_seconds = .1
            room = Room(game, 1500)
            room.encoder = PacketEncoder(gop=3)
            chunks = []

            def ready(n, url, end):
                chunks.append((n, url, end))
                room.broadcast_json({'t':'chunk', 'n':n, 'url':url, 'end':end})

            room.publisher = ChunkPublisher(LocalSink(data), 'test', ready)
            game.on_quiet = lambda reason: loop.call_soon_threadsafe(room.on_quiet, reason)
            app = make_app(room, game, game.emu, 'local-test', '', str(Path(data) / 'settings.json'))
            game.start()
            try:
                async with TestClient(TestServer(app)) as client:
                    async with client.ws_connect('/ws') as ws:
                        await ws.receive_json(timeout=1)
                        message = await ws.receive_json(timeout=1)
                        self.assertEqual(message['t'], 'chunk')
                        self.assertFalse(game.quiet)
                        self.assertFalse(game.active())
                        self.assertGreater(game.seq, 0)
                    await asyncio.sleep(.25)
                    self.assertTrue(game.quiet)
                    self.assertTrue(room.publisher.latest[2])
                    frozen_frame = game.frame_no
                    frozen_chunks = len(chunks)
                    await asyncio.sleep(.1)
                    self.assertEqual(game.frame_no, frozen_frame)
                    self.assertEqual(len(chunks), frozen_chunks)
                    async with client.ws_connect('/ws') as ws:
                        await ws.receive_json(timeout=1)
                        await asyncio.sleep(.08)
                        self.assertFalse(game.quiet)
                        self.assertGreater(game.frame_no, frozen_frame)
                        self.assertFalse(game.steps)
                        self.assertEqual(game.meta['frames'], 0)
            finally:
                game.running = False
                game.poke()
                await asyncio.to_thread(game.join, 1)
                await asyncio.sleep(.05)
                await room.publisher.close()

    async def test_direct_join_gets_picture_without_vote(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(FakeCore(), data, lambda *args: None)
            room = Room(game, 1500)
            app = make_app(room, game, game.emu, 'local-test', '', str(Path(data) / 'settings.json'))
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect('/ws') as ws:
                    hello = await ws.receive_json(timeout=1)
                    self.assertEqual(hello['video'], 'ws')
                    packet = await ws.receive_bytes(timeout=1)
                    self.assertEqual(packet[0], 1)
                    self.assertTrue(game.viewers.is_set())
                    self.assertIsNone(game.quiet_reason())
                room.last_frame = (42, np.full((160, 240), 123, dtype=np.uint16))
                async with client.ws_connect('/ws') as ws:
                    await ws.receive_json(timeout=1)
                    packet = await ws.receive_bytes(timeout=1)
                    self.assertEqual(struct.unpack_from('<I', packet, 3)[0], 42)

    async def test_step_multiplier_requires_toggle(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(FakeCore(), data, lambda *args: None)
            room = Room(game, 1500)
            from run import Viewer
            viewer = Viewer(None, '127.0.0.1')
            if not hasattr(room, 'steps_on'):
                self.skipTest('step toggle belongs to PR2')
            room.handle_vote(viewer, 'up', 5)
            self.assertEqual(room.close_window(), ('up', 1))
            room.steps_on = True
            room.handle_vote(viewer, 'up', 5)
            self.assertEqual(room.close_window(), ('up', 5))

    async def test_empty_boot_saves_once_and_viewer_wakes_without_vote(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(FakeCore(), data, lambda *args: None)
            calls = []
            game.last_save = time.time() - 10
            game.autosave = lambda: calls.append('saved')
            game.start()
            try:
                await asyncio.sleep(.6)
                self.assertEqual(calls, ['saved'])
                game.set_viewers(True)
                self.assertFalse(game.active())
                self.assertIsNone(game.quiet_reason())
                game.admin_paused = True
                self.assertEqual(game.quiet_reason(), 'admin')
            finally:
                game.running = False
                game.wake.set()
                await asyncio.to_thread(game.join, 1)

    async def test_partial_chunk_and_cleanup_keep_latest(self):
        with tempfile.TemporaryDirectory() as data:
            announced = []
            publisher = ChunkPublisher(LocalSink(data), 'test', lambda *args: announced.append(args), ttl=1)
            publisher.add(1, FakeCore.frame, b'')
            publisher.flush()
            await asyncio.sleep(.1)
            self.assertEqual(announced[0][2], True)
            newest = publisher._key(publisher.latest[0])
            self.assertNotIn(newest, publisher.expired(time.monotonic() + 100))
            self.assertTrue((Path(data) / newest).exists())
            await publisher.close()
            self.assertFalse((Path(data) / newest).exists())

    async def test_host_paused_fallback_publishes_current_picture(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(FakeCore(), data, lambda *args: None)
            game.quiet = game.admin_paused = True
            room = Room(game, 1500)
            if not hasattr(room, 'on_relay_change'):
                self.skipTest('relay belongs to PR3')
            from relay import RelayPublisher
            room.publisher = ChunkPublisher(LocalSink(data), 'test', lambda *args: None)
            room.relay = RelayPublisher('http://127.0.0.1:8799', 'local-test', 'test', room.encoder)
            room.relay.up = True
            room.last_frame = (42, np.full((160, 240), 123, dtype=np.uint16))
            room.on_relay_change(True)
            self.assertFalse(room.publisher.enabled)
            self.assertEqual(room.relay._queue.get_nowait()[0], 0x10)
            self.assertEqual(room.relay._queue.get_nowait(), b'\x11')
            room.relay.up = False
            room.on_relay_change(False)
            await asyncio.sleep(.1)
            latest = room.publisher.latest
            self.assertIsNotNone(latest)
            self.assertTrue(latest[2])
            chunk = (Path(data) / room.publisher._key(latest[0])).read_bytes()
            packet_len = struct.unpack_from('<I', chunk, 10)[0]
            packet = chunk[14:14 + packet_len]
            self.assertEqual(struct.unpack_from('<I', packet, 3)[0], 42)
            self.assertEqual(zlib.decompress(packet[7:]), room.last_frame[1].tobytes())
            self.assertEqual(game.frame_no, 0)
            self.assertFalse(game.wake.is_set())
            await room.publisher.close()

    async def test_watching_keeps_frames_running_without_inputs_or_clock_time(self):
        with tempfile.TemporaryDirectory() as data:
            packets = []
            game = GameLoop(FakeCore(), data, lambda *args: packets.append(args))
            game.last_input = time.monotonic() - 3600
            game.set_viewers(True)
            game.start()
            try:
                await asyncio.sleep(.15)
                self.assertGreaterEqual(len(packets), 3)
                self.assertFalse(game.quiet)
                self.assertEqual(game.meta['frames'], 0)
                self.assertFalse(game.steps)
            finally:
                game.running = False
                game.poke()
                await asyncio.to_thread(game.join, 1)

    async def test_empty_room_grace_freezes_at_30_seconds_and_rejoin_wakes(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(FakeCore(), data, lambda *args: None)
            self.assertEqual(game.quiet_reason(), 'empty')
            with patch('run.time.monotonic', return_value=100):
                game.set_viewers(True)
                self.assertIsNone(game.quiet_reason())
                game.enqueue('up')
                self.assertTrue(game.clock_running())
                game.set_viewers(False)
                self.assertIsNone(game.quiet_reason())
                self.assertFalse(game.clock_running())
            with patch('run.time.monotonic', return_value=129.99):
                self.assertIsNone(game.quiet_reason())
            with patch('run.time.monotonic', return_value=130):
                self.assertEqual(game.quiet_reason(), 'empty')
                game.set_viewers(True)
                self.assertIsNone(game.quiet_reason())
                game.admin_paused = True
                self.assertEqual(game.quiet_reason(), 'admin')

    async def test_clock_idle_is_separate_from_video_paused(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(FakeCore(), data, lambda *args: None)
            room = Room(game, 1500)
            game.set_viewers(True)
            state = room.state_msg(None)
            self.assertTrue(state['idle'])
            self.assertTrue(state['paused'])
            self.assertTrue(state['playing'])
            game.admin_paused = True
            self.assertFalse(room.state_msg(None)['playing'])

    async def test_changing_mode_lock_clears_current_countdown_and_saves_next_duration(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'XP_ADMIN_HOST': '*'}):
            game = GameLoop(FakeCore(), data, lambda *args: None)
            room = Room(game, 1500)
            room.mode_locked_until = time.monotonic() + 600
            room._flip_since = time.monotonic()
            events = []
            room.broadcast_json = events.append
            settings = Path(data) / 'settings.json'
            app = make_app(room, game, game.emu, 'local-test', '', str(settings))
            async with TestClient(TestServer(app)) as client:
                response = await client.post('/admin/api/config', json={'modeLock': 60}, headers={'x-admin-token':'local-test'})
                self.assertEqual(response.status, 200)
                self.assertEqual(room.mode_locked_until, 0)
                self.assertIsNone(room._flip_since)
                self.assertEqual(json.loads(settings.read_text())['modeLock'], 60)
                self.assertIn({'t':'modelock', 'lockMs':0}, events)
                room.set_mode('anarchy')
                self.assertGreater(room.mode_locked_until, time.monotonic() + 59)
                deadline = room.mode_locked_until
                response = await client.post('/admin/api/config', json={'modeLock':60}, headers={'x-admin-token':'local-test'})
                self.assertEqual(response.status, 200)
                self.assertEqual(room.mode_locked_until, deadline)
                self.assertEqual(events[-1]['t'], 'modechange')
                self.assertGreater(events[-1]['lockMs'], 59000)


if __name__ == '__main__':
    unittest.main()
