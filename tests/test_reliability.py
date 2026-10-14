"""Mode identity and save-failure regression checks; fake core and disposable disk only."""
import asyncio
import errno
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from run import (AUTOSAVE_SECONDS, MODE_HOLD_SECONDS, MODE_LOCK_SECONDS, PER_IP_VOTE_CAP,
                 GameLoop, Room, Viewer, atomic_write, make_app)


class SaveCore:
    fps = 60
    sample_rate = 32768
    frame = np.zeros((160, 240), dtype=np.uint16)
    pressed = set()

    def __init__(self):
        self.frames = 0

    def run_frame(self):
        self.frames += 1

    def drain_audio(self):
        return np.zeros(512, dtype=np.int16)

    def read(self, *args):
        raise ValueError('fake core has no game memory')

    def save_state(self):
        return f'state:{self.frames}'.encode()

    def save_ram(self):
        return f'sram:{self.frames}'.encode()

    def frame_rgb(self):
        return np.zeros((160, 240, 3), dtype=np.uint8)


class ModeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.room = Room(None, 1500)

    def voter(self, identity, ip, preference=None):
        viewer = Viewer(None, ip)
        viewer.id = identity
        self.room.viewers.add(viewer)
        if preference:
            self.room.handle_mode(viewer, preference)
        return viewer

    def leave(self, viewer):
        self.room.viewers.remove(viewer)
        self.room.forget_mode_voter(viewer.id)

    def test_ten_same_browser_tabs_cannot_overpower_three_democracy_voters(self):
        for _ in range(10):
            self.voter('same-browser', 'attacker', 'anarchy')
        for n in range(3):
            self.voter(f'voter-{n}', f'ip-{n}', 'democracy')
        for now in (100, 100 + MODE_HOLD_SECONDS + 1):
            with patch('run.time.monotonic', return_value=now):
                self.assertEqual(self.room.update_mode(), .25)
        self.assertEqual(self.room.mode, 'democracy')
        self.assertEqual(self.room.mode_voters, 4)
        self.assertEqual(len(self.room.viewers), 13)

    def test_unique_ids_from_one_ip_are_capped_in_stable_identity_order(self):
        for n in reversed(range(10)):
            self.voter(f'fake-{n}', 'shared-ip', 'anarchy' if n < 3 else 'democracy')
        for n in range(3):
            self.voter(f'other-{n}', f'ip-{n}', 'democracy')
        self.assertEqual(self.room.update_mode(), .5)
        self.assertEqual(self.room.mode_voters, PER_IP_VOTE_CAP + 3)

    def test_same_id_across_ips_counts_once_and_cannot_move_cap_bucket(self):
        peers = []
        for n in range(4):
            self.voter(f'fake-{n}', 'first-ip', 'anarchy')
            peers.append(self.voter(f'fake-{n}', 'second-ip', 'anarchy'))
        self.voter('honest', 'third-ip', 'democracy')
        self.assertEqual(self.room.update_mode(), .75)
        self.assertEqual(self.room.mode_voters, 4)
        for peer in peers:
            self.room.handle_mode(peer, 'democracy')
            self.room.handle_mode(peer, 'anarchy')
        self.assertEqual(self.room.update_mode(), .75)
        self.assertEqual({ip for identity, (_, ip) in self.room.mode_votes.items()
                          if identity.startswith('fake-')}, {'first-ip'})

    def test_latest_choice_is_shared_until_last_tab_leaves(self):
        first = self.voter('browser', 'ip', 'anarchy')
        second = self.voter('browser', 'ip', 'democracy')
        self.assertEqual(self.room.update_mode(), 0)
        self.leave(second)
        self.assertEqual(self.room.update_mode(), 0)  # no resurrection of first tab's old choice
        self.room.handle_mode(first, 'anarchy')
        self.assertEqual(self.room.update_mode(), 1)
        self.leave(first)
        self.assertIsNone(self.room.update_mode())
        self.assertEqual(self.room.mode_voters, 0)
        self.voter('browser', 'ip')
        self.assertIsNone(self.room.update_mode())  # a reconnect must opt in again

    def test_majority_hold_resets_and_lock_requires_fresh_hold(self):
        voters = [self.voter(f'voter-{n}', f'ip-{n}', 'anarchy' if n < 3 else 'democracy')
                  for n in range(5)]
        with patch('run.time.monotonic', return_value=100):
            self.assertEqual(self.room.update_mode(), .6)
        with patch('run.time.monotonic', return_value=100 + MODE_HOLD_SECONDS - .01):
            self.room.update_mode()
            self.assertEqual(self.room.mode, 'democracy')
            self.room.handle_mode(voters[0], 'democracy')
            self.room.update_mode()
            self.assertIsNone(self.room._flip_since)
            self.room.handle_mode(voters[0], 'anarchy')
        with patch('run.time.monotonic', return_value=120):
            self.room.update_mode()
        with patch('run.time.monotonic', return_value=120 + MODE_HOLD_SECONDS):
            self.room.update_mode()
            self.assertEqual(self.room.mode, 'anarchy')
            self.assertEqual(self.room.mode_locked_until, 120 + MODE_HOLD_SECONDS + MODE_LOCK_SECONDS)
        for voter in voters:
            self.room.handle_mode(voter, 'democracy')
        deadline = self.room.mode_locked_until
        with patch('run.time.monotonic', return_value=deadline - .01):
            self.room.update_mode()
            self.assertIsNone(self.room._flip_since)
        with patch('run.time.monotonic', return_value=deadline):
            self.room.update_mode()
            self.assertEqual(self.room.mode, 'anarchy')
        with patch('run.time.monotonic', return_value=deadline + MODE_HOLD_SECONDS):
            self.room.update_mode()
            self.assertEqual(self.room.mode, 'democracy')


class AtomicSaveTests(unittest.TestCase):
    def test_partial_write_and_replace_failures_preserve_previous_complete_files(self):
        original_temp = tempfile.NamedTemporaryFile

        class PartialWrite:
            def __enter__(self):
                self.file = original_temp(dir=data, prefix='.save-', delete=False)
                self.name = self.file.name
                return self

            def write(self, blob):
                self.file.write(blob[:3])
                self.file.flush()
                raise OSError(errno.ENOSPC, 'injected disk full')

            def __exit__(self, *args):
                self.file.close()

        with tempfile.TemporaryDirectory() as data:
            for name in ('latest.st', 'game.srm', 'meta.json', '123.st', '123.png'):
                with self.subTest(file=name):
                    path = Path(data) / name
                    path.write_bytes(b'last complete file')
                    with patch('run.tempfile.NamedTemporaryFile', side_effect=lambda **kw: PartialWrite()):
                        with self.assertRaises(OSError):
                            atomic_write(str(path), b'replacement')
                    self.assertEqual(path.read_bytes(), b'last complete file')
                    with patch('run.os.replace', side_effect=OSError(errno.EIO, 'injected rename failure')):
                        with self.assertRaises(OSError):
                            atomic_write(str(path), b'replacement')
                    self.assertEqual(path.read_bytes(), b'last complete file')
                    self.assertEqual(list(Path(data).glob('.save-*')), [])

    def test_sram_disk_failure_preserves_good_sram_and_recovers(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(SaveCore(), data, lambda *args: None)
            game.autosave()
            sram = Path(data) / 'game.srm'
            original_replace = os.replace

            def fail_sram(source, destination):
                if str(destination) == str(sram):
                    raise OSError(errno.ENOSPC, 'injected SRAM disk full')
                return original_replace(source, destination)

            game.emu.frames = 42
            with patch('run.os.replace', side_effect=fail_sram):
                with self.assertRaises(OSError):
                    game.autosave()
            self.assertEqual(sram.read_bytes(), b'sram:0')
            self.assertEqual(Path(game.latest_path()).read_bytes(), b'state:42')
            game.autosave()
            self.assertEqual(sram.read_bytes(), b'sram:42')


async def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition did not become true')
        await asyncio.sleep(.01)


class ReliabilityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def stop(self, game):
        game.running = False
        game.poke()
        await asyncio.to_thread(game.join, 1)
        self.assertFalse(game.is_alive())

    def app(self, game, data):
        room = Room(game, 1500)
        return room, make_app(room, game, game.emu, 'local-test', '', str(Path(data) / 'settings.json'))

    async def test_websocket_identity_change_and_disconnect_do_not_leave_ghost_votes(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(SaveCore(), data, lambda *args: None)
            room, app = self.app(game, data)
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect('/ws') as first, client.ws_connect('/ws') as second:
                    for ws in (first, second):
                        await ws.receive_json(timeout=1)
                        await ws.receive_bytes(timeout=1)
                        await ws.send_json({'t': 'hello', 'id': 'browser-one'})
                        await ws.send_json({'t': 'mode', 'm': 'anarchy'})
                    await eventually(lambda: len(room.viewers) == 2 and
                                     all(v.id == 'browser-one' for v in room.viewers) and
                                     'browser-one' in room.mode_votes)
                    room.update_mode()
                    self.assertEqual(room.mode_voters, 1)
                    await first.send_json({'t': 'hello', 'id': 'browser-two'})
                    await eventually(lambda: {v.id for v in room.viewers} == {'browser-one', 'browser-two'})
                    self.assertIn('browser-one', room.mode_votes)
                    await second.close()
                    await eventually(lambda: len(room.viewers) == 1)
                    self.assertNotIn('browser-one', room.mode_votes)
                    await first.send_json({'t': 'mode', 'm': 'democracy'})
                    await eventually(lambda: 'browser-two' in room.mode_votes)
                    await first.send_json({'t': 'hello', 'id': 'browser-three'})
                    await eventually(lambda: not room.mode_votes)
                await eventually(lambda: not room.viewers)
                self.assertIsNone(room.update_mode())

    async def test_periodic_disk_failure_keeps_frames_retries_recovers_and_reports_status(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'XP_ADMIN_HOST': '*'}):
            game = GameLoop(SaveCore(), data, lambda *args: None)
            game.autosave()
            previous = Path(game.latest_path()).read_bytes()
            game.last_save = time.time() - AUTOSAVE_SECONDS - 1
            game.set_viewers(True)
            _, app = self.app(game, data)
            original_replace = os.replace
            attempts = []
            fail = True

            def disk_full(source, destination):
                if destination == game.latest_path():
                    attempts.append(time.monotonic())
                    if fail:
                        raise OSError(errno.ENOSPC, 'private/path should not be public')
                return original_replace(source, destination)

            with patch('run.os.replace', side_effect=disk_full), patch('run.SAVE_RETRY_SECONDS', .15):
                game.start()
                try:
                    async with TestClient(TestServer(app)) as client:
                        await eventually(lambda: game.save_failures >= 2)
                        self.assertTrue(game.is_alive())
                        self.assertGreater(game.frame_no, 5)
                        self.assertGreaterEqual(attempts[1] - attempts[0], .14)
                        self.assertEqual(Path(game.latest_path()).read_bytes(), previous)
                        response = await client.get('/healthz')
                        self.assertEqual(response.status, 200)
                        self.assertEqual(await response.text(), 'ok')
                        response = await client.get('/admin/api/status', headers={'x-admin-token': 'local-test'})
                        status = await response.json()
                        self.assertTrue(status['gameAlive'])
                        self.assertTrue(status['persistence']['degraded'])
                        self.assertEqual(status['persistence']['error'], 'OSError')
                        self.assertNotIn('private/path', str(status))
                        fail = False
                        await eventually(lambda: game.save_error is None)
                        self.assertEqual(game.save_failures, 0)
                        self.assertGreater(game.last_save_success, game.last_save_attempt)
                        self.assertNotEqual(Path(game.latest_path()).read_bytes(), previous)
                        count = len(attempts)
                        await asyncio.sleep(.2)
                        self.assertEqual(len(attempts), count)
                        response = await client.get('/admin/api/status', headers={'x-admin-token': 'local-test'})
                        self.assertFalse((await response.json())['persistence']['degraded'])
                finally:
                    await self.stop(game)

    async def test_failed_quiet_and_host_pause_saves_retry_without_frames_then_stop_retrying(self):
        for paused in (False, True):
            with self.subTest(host_paused=paused), tempfile.TemporaryDirectory() as data:
                game = GameLoop(SaveCore(), data, lambda *args: None)
                game.admin_paused = paused
                if paused:
                    game.set_viewers(True)
                game.last_save = time.time() - 10
                original_replace = os.replace
                attempts = []
                fail = True

                def disk_full(source, destination):
                    if destination == game.latest_path():
                        attempts.append(time.monotonic())
                        if fail:
                            raise OSError(errno.ENOSPC, 'injected disk full')
                    return original_replace(source, destination)

                with patch('run.os.replace', side_effect=disk_full), patch('run.SAVE_RETRY_SECONDS', .15):
                    game.start()
                    try:
                        await eventually(lambda: game.save_failures >= 2)
                        self.assertTrue(game.quiet)
                        self.assertTrue(game.is_alive())
                        self.assertEqual(game.frame_no, 0)
                        self.assertGreaterEqual(attempts[1] - attempts[0], .14)
                        fail = False
                        await eventually(lambda: game.save_error is None)
                        self.assertIsNotNone(game.last_save_success)
                        count = len(attempts)
                        await asyncio.sleep(.55)
                        self.assertEqual(len(attempts), count)
                        self.assertEqual(game.frame_no, 0)
                    finally:
                        await self.stop(game)

    async def test_health_distinguishes_quiet_stopped_and_dead_thread(self):
        with tempfile.TemporaryDirectory() as data:
            game = GameLoop(SaveCore(), data, lambda *args: None)
            _, app = self.app(game, data)
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get('/healthz')).status, 503)
                game.start()
                try:
                    await eventually(lambda: game.quiet)
                    self.assertEqual((await client.get('/healthz')).status, 200)
                    game.running = False
                    response = await client.get('/healthz')
                    self.assertEqual(response.status, 503)
                    self.assertEqual(await response.text(), 'unavailable')
                finally:
                    await self.stop(game)
                game.running = True  # an unexpected thread exit can leave this flag set
                self.assertFalse(game.is_alive())
                self.assertEqual((await client.get('/healthz')).status, 503)


if __name__ == '__main__':
    unittest.main()
