"""Direction timing, bounded Anarchy inputs, and Democracy renewal; fake core only."""
import sys
import tempfile
import unittest
import collections
import json
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from run import (ANARCHY_IP_INPUT_CAP, ANARCHY_QUEUE_CAP, GameLoop, Room, Viewer, make_app)


class NamingKeyboard:
    """FireRed ReadKeys timing: naming-screen start delay 16, repeat delay 5.

    Source: pret/pokefirered src/main.c ReadKeys and src/naming_screen.c NamingScreen_Init.
    No ROM or game save is required.
    """
    fps = 60
    pressed = set()

    def __init__(self):
        self.previous = set()
        self.repeat_counter = 16
        self.moves = []

    def run_frame(self):
        new = self.pressed - self.previous
        if self.pressed and self.previous == self.pressed:
            self.repeat_counter -= 1
            if self.repeat_counter == 0:
                new = self.pressed
                self.repeat_counter = 5
        else:
            self.repeat_counter = 16
        self.moves.extend(sorted(new))
        self.previous = self.pressed.copy()


class InputTests(unittest.TestCase):
    def play_vote(self, direction, count=1):
        with tempfile.TemporaryDirectory() as data:
            core = NamingKeyboard()
            game = GameLoop(core, data, lambda *args: None)
            room = Room(game, 1500)
            room.steps_on = count > 1
            room.handle_vote(Viewer(None, '127.0.0.1'), direction, count)
            room.press(*room.close_window())
            while game.steps or game.current is not None:
                game._apply_input()
                core.run_frame()
            self.assertEqual(core.pressed, set())
            return core.moves

    def test_single_direction_vote_moves_naming_cursor_once(self):
        for direction in ('up', 'down', 'left', 'right'):
            with self.subTest(direction=direction):
                self.assertEqual(self.play_vote(direction), [direction])

    def test_explicit_multiplier_moves_requested_number_of_times(self):
        for count in (2, 3, 5):
            with self.subTest(count=count):
                self.assertEqual(self.play_vote('up', count), ['up'] * count)

    def test_previous_17_frame_hold_reproduces_double_input(self):
        core = NamingKeyboard()
        core.pressed = {'up'}
        for _ in range(17):
            core.run_frame()
        self.assertEqual(core.moves, ['up', 'up'])


class AnarchyHoldTests(unittest.TestCase):
    def setUp(self):
        class ConsumingGame:
            def __init__(self):
                self.steps = collections.deque()
                self.inputs = []

            def enqueue(self, button, count):
                self.inputs.append((button, count))

        self.game = ConsumingGame()
        self.room = Room(self.game, 1500)
        self.room.mode = 'anarchy'
        self.viewer = self.voter('browser', 'ip')

    def voter(self, identity, ip):
        viewer = Viewer(None, ip)
        viewer.id = identity
        self.room.viewers.add(viewer)
        return viewer

    def press(self, now, viewer=None, button='up'):
        with patch('run.time.monotonic', return_value=now):
            self.room.handle_vote(viewer or self.viewer, button, 1)

    def test_continuous_hold_stops_and_rejected_spam_requires_a_quiet_pause(self):
        for n in range(10):
            self.press(100 + n * .3)
        self.assertEqual(self.game.inputs, [('up', 1)] * 10)
        for now in (103, 103.3, 104, 105):
            self.press(now)
        self.assertEqual(len(self.game.inputs), 10)
        notices = [item for item in self.viewer.queue._queue
                   if item[0] == 'json' and 'inputlimit' in item[1]]
        self.assertTrue(notices)
        self.press(107)
        self.assertEqual(len(self.game.inputs), 11)

    def test_duplicate_tabs_share_rate_and_burst_limits_across_ips(self):
        duplicate = self.voter('browser', 'other-ip')
        for n in range(20):
            self.press(100 + n * .15, self.viewer if n % 2 == 0 else duplicate)
        self.assertEqual(len(self.game.inputs), 10)
        self.press(103, duplicate)
        reconnected = self.voter('browser', 'third-ip')
        self.press(103.1, reconnected)
        self.assertEqual(len(self.game.inputs), 10)
        self.press(105.1, reconnected)
        self.assertEqual(len(self.game.inputs), 11)

    def test_changing_keys_or_hello_identity_does_not_extend_connection_burst(self):
        for n in range(10):
            self.viewer.id = f'rotated-{n}'
            self.press(100 + n * .3, button='up' if n % 2 else 'a')
        self.viewer.id = 'another-identity'
        self.press(103, button='down')
        self.assertEqual(len(self.game.inputs), 10)
        self.assertNotIn('another-identity', self.room.anarchy_by_id)

    def test_rotated_connections_are_bounded_by_ip_and_other_ips_can_play(self):
        for n in range(ANARCHY_IP_INPUT_CAP):
            self.press(100 + n * .01, self.voter(f'rotated-{n}', 'ip'))
        self.assertEqual(len(self.game.inputs), ANARCHY_IP_INPUT_CAP)
        self.press(101, self.voter('over-limit', 'ip'))
        self.assertEqual(len(self.game.inputs), ANARCHY_IP_INPUT_CAP)
        self.assertNotIn('over-limit', self.room.anarchy_by_id)
        self.press(101, self.voter('honest', 'different-ip'))
        self.assertEqual(len(self.game.inputs), ANARCHY_IP_INPUT_CAP + 1)
        self.press(106, self.voter('after-window', 'ip'))
        self.assertEqual(len(self.game.inputs), ANARCHY_IP_INPUT_CAP + 2)

    def test_queue_cap_still_bounds_pending_inputs(self):
        self.game.steps.extend([None] * ANARCHY_QUEUE_CAP)
        for n in range(100):
            self.viewer.id = f'rejected-{n}'
            self.press(100 + n * .001)
        self.assertEqual(self.game.inputs, [])
        self.assertEqual(self.room.anarchy_by_id, {})
        self.game.steps.clear()
        self.press(100.3)
        self.assertEqual(self.game.inputs, [('up', 1)])

    def test_expired_identity_and_ip_records_are_pruned(self):
        self.press(100)
        self.press(161, self.voter('fresh-browser', 'fresh-ip'))
        self.assertEqual(set(self.room.anarchy_by_id), {'fresh-browser'})
        self.assertEqual(set(self.room.anarchy_by_ip), {'fresh-ip'})

    def test_democracy_hold_is_one_vote_and_can_renew_after_each_window(self):
        self.room.mode = 'democracy'
        for n in range(20):
            self.press(100 + n * .3)
        self.assertEqual(dict(self.room.tally()), {'up:1': 1})
        self.assertEqual(self.room.close_window(), ('up', 1))
        self.assertEqual(len(self.room.votes), 0)
        self.press(106)
        self.assertEqual(dict(self.room.tally()), {'up:1': 1})
        self.assertEqual(self.game.inputs, [])

    def test_unlimited_override_allows_long_holds_then_limits_again_when_disabled(self):
        self.room.anarchy_hold_unlimited = True
        for n in range(40):
            self.press(100 + n * .3)
        self.assertEqual(len(self.game.inputs), 40)
        self.room.anarchy_hold_unlimited = False
        self.press(112)
        self.assertEqual(len(self.game.inputs), 40)
        self.press(114)
        self.assertEqual(len(self.game.inputs), 41)

    def test_unlimited_override_keeps_shared_rate_ip_volume_and_queue_limits(self):
        self.room.anarchy_hold_unlimited = True
        duplicate = self.voter('browser', 'other-ip')
        self.press(100)
        self.press(100.1, duplicate)
        self.assertEqual(len(self.game.inputs), 1)
        for n in range(ANARCHY_IP_INPUT_CAP - 1):
            self.press(100.2 + n * .01, self.voter(f'rotated-{n}', 'ip'))
        self.press(101, self.voter('over-limit', 'ip'))
        self.assertEqual(len(self.game.inputs), ANARCHY_IP_INPUT_CAP)
        self.game.steps.extend([None] * ANARCHY_QUEUE_CAP)
        self.press(102, self.voter('queue-full', 'fresh-ip'))
        self.assertEqual(len(self.game.inputs), ANARCHY_IP_INPUT_CAP)


class UnlimitedHoldAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_toggle_persists_and_reaches_existing_and_new_viewers(self):
        class SettingsCore(NamingKeyboard):
            sample_rate = 32768
            frame = np.zeros((160, 240), dtype=np.uint16)

        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'XP_ADMIN_HOST': 'admin.example.com'}):
            game = GameLoop(SettingsCore(), data, lambda *args: None)
            room = Room(game, 1500)
            events = []
            room.broadcast_json = events.append
            settings = Path(data) / 'settings.json'
            app = make_app(room, game, game.emu, 'local-test', '', str(settings))
            allowed = {'host': 'admin.example.com', 'cf-access-jwt-assertion': 'local-fixture',
                       'x-admin-token': 'local-test'}
            async with TestClient(TestServer(app)) as client:
                response = await client.post('/admin/api/config', json={'anarchyHoldUnlimited': True},
                                             headers={'host': 'play.example.com', 'x-admin-token': 'local-test'})
                self.assertEqual(response.status, 404)
                response = await client.post('/admin/api/config', json={'anarchyHoldUnlimited': True},
                                             headers={**allowed, 'x-admin-token': 'wrong-token'})
                self.assertEqual(response.status, 403)
                self.assertFalse(room.anarchy_hold_unlimited)
                response = await client.post('/admin/api/config', json={'anarchyHoldUnlimited': True}, headers=allowed)
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())['anarchyHoldUnlimited'])
                self.assertTrue(json.loads(settings.read_text())['anarchyHoldUnlimited'])
                self.assertEqual(events, [{'t': 'holdsetting', 'anarchyHoldUnlimited': True}])
                response = await client.get('/admin/api/status', headers=allowed)
                self.assertTrue((await response.json())['settings']['anarchyHoldUnlimited'])
                self.assertTrue(room.state_msg(None)['anarchyHoldUnlimited'])
                ws = await client.ws_connect('/ws', headers={'host': 'play.example.com'})
                hello = await ws.receive_json()
                self.assertEqual(hello['t'], 'hello')
                self.assertTrue(hello['anarchyHoldUnlimited'])
                await ws.close()
                response = await client.post('/admin/api/config', json={'anarchyHoldUnlimited': True}, headers=allowed)
                self.assertEqual(response.status, 200)
                self.assertEqual(len(events), 1)  # same setting must not replay the event
                response = await client.post('/admin/api/config', json={'anarchyHoldUnlimited': False}, headers=allowed)
                self.assertEqual(response.status, 200)
                self.assertFalse(room.anarchy_hold_unlimited)
                self.assertFalse(json.loads(settings.read_text())['anarchyHoldUnlimited'])
                response = await client.post('/admin/api/config', json={'anarchyHoldUnlimited': 'false', 'window': 500}, headers=allowed)
                self.assertEqual(response.status, 400)
                self.assertEqual(room.window_ms, 1500)
                self.assertFalse(room.anarchy_hold_unlimited)


if __name__ == '__main__':
    unittest.main()
