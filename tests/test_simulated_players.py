"""Opt-in simulated inputs: deterministic clocks, fake core, disposable local HTTP."""
import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from run import (ANARCHY_QUEUE_CAP, GameLoop, Room, SIMULATED_BUTTONS,
                 SimulatedPlayer, Viewer, load_settings, make_app)
from test_reliability import SaveCore


class SimulatedPlayerTests(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.game = GameLoop(SaveCore(), self.storage.name, lambda *args: None)
        self.room = Room(self.game, 1500)
        self.viewer = Viewer(None, 'human-ip')
        self.viewer.id = 'human-browser'
        self.room.viewers.add(self.viewer)
        self.game.set_viewers(True)

    def tick(self, now):
        with patch('run.time.monotonic', return_value=now), patch('run.random.choice', return_value='a'):
            self.room.tick_simulated_players(now)

    def test_default_off_and_no_synthetic_connections_or_transport(self):
        self.tick(100)
        self.tick(1000)
        self.assertEqual(self.room.state_msg(None)['simulatedPlayers'], 0)
        self.assertFalse(self.room.votes)
        self.room.set_simulated_players(50)
        self.assertEqual(self.room.state_msg(None)['viewers'], 1)
        self.assertEqual(self.room.state_msg(None)['simulatedPlayers'], 50)
        self.assertEqual(self.room.viewers, {self.viewer})
        self.assertEqual(len({p.id for p in self.room.simulated_players}), 50)
        for player in self.room.simulated_players:
            self.assertEqual(uuid.UUID(player.id).hex, player.id)
            self.assertTrue(player.ip.startswith('simulated:'))
            self.assertFalse(hasattr(player, 'queue'))
            self.assertFalse(hasattr(player, 'ws'))
            self.room.send_json(player, {'t': 'inputlimit'})
        self.room.broadcast_packet(1, SaveCore.frame, b'')
        self.assertEqual(self.viewer.video_pending, 1)
        self.assertEqual(self.viewer.queue.qsize(), 1)

    def test_rate_scales_and_caps_with_one_attempt_per_tick(self):
        attempts_by_count = []
        for count in (1, 2, 4, 8, 50):
            self.room.set_simulated_players(count)
            with patch.object(self.room, 'handle_vote') as vote:
                for i in range(161):
                    self.tick(100 + i * .25)
                attempts_by_count.append(vote.call_count)
                self.assertTrue(all(call.args[1:] == ('a', 1) for call in vote.call_args_list))
        self.assertEqual(attempts_by_count, [10, 20, 40, 80, 80])

    def test_time_jump_never_replays_missed_inputs(self):
        self.room.set_simulated_players(50)
        with patch.object(self.room, 'handle_vote') as vote:
            self.tick(100)
            self.tick(10000)
            self.assertEqual(vote.call_count, 1)
            self.tick(10000)
            self.tick(10000.25)
            self.assertEqual(vote.call_count, 1)
            self.tick(10000.5)
            self.assertEqual(vote.call_count, 2)

    def test_round_robin_votes_use_existing_window_and_stable_identities(self):
        self.room.set_simulated_players(2)
        identities = [p.id for p in self.room.simulated_players]
        self.room.handle_vote(self.viewer, 'a', 1)
        for now in (100, 102, 104, 106, 108):
            self.tick(now)
        self.assertEqual(set(self.room.votes), {*identities, self.viewer.id})
        self.assertEqual(dict(self.room.tally()), {'a:1': 3})
        self.assertEqual(self.room.close_window(), ('a', 1))
        self.assertFalse(self.room.votes)
        self.tick(110)
        self.assertEqual(len(self.room.votes), 1)
        self.room.set_simulated_players(3)
        self.assertEqual([p.id for p in self.room.simulated_players[:2]], identities)

    def test_ticker_delivers_democracy_winner_and_stops_bot_votes_when_disabled(self):
        self.room.set_simulated_players(8)
        self.room.window_end = 101.5
        now = 100
        wins_at_disable = None

        def wins():
            return [json.loads(payload) for kind, payload in self.viewer.queue._queue
                    if kind == 'json' and json.loads(payload)['t'] == 'win']

        async def advance_clock(_):
            nonlocal now, wins_at_disable
            now += .25
            if now == 103:
                wins_at_disable = len(wins())
                self.room.set_simulated_players(0)
                self.game.steps.clear()  # fake core consumes the already accepted press
            if now > 106:
                raise asyncio.CancelledError

        with patch('run.time.monotonic', side_effect=lambda: now), \
                patch('run.random.choice', return_value='a'), \
                patch('run.asyncio.sleep', side_effect=advance_clock):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.room.ticker_loop())
        self.assertGreater(wins_at_disable, 0)
        self.assertEqual(len(wins()), wins_at_disable)
        self.assertEqual(self.room.total_inputs, 0)  # bot-only winners stay out of the admin count
        self.assertFalse(self.room.votes)
        self.assertFalse(self.game.steps)
        first = wins()[0]
        self.assertEqual((first['b'], first['n'], first['mode'], first['you']),
                         ('a', 1, 'democracy', False))

    def test_reduction_and_zero_clear_removed_votes_and_limiter_memory(self):
        self.room.set_simulated_players(50)
        for player in self.room.simulated_players:
            self.room.handle_vote(player, 'a', 1)
        self.room.handle_vote(self.viewer, 'b', 1)
        keep = self.room.simulated_players[0]
        self.room.set_simulated_players(1)
        self.assertEqual(set(self.room.votes), {keep.id, self.viewer.id})
        self.room.mode = 'anarchy'
        self.tick(100)
        self.tick(104)
        self.assertIn(keep.id, self.room.anarchy_by_id)
        self.room.set_simulated_players(0)
        self.assertEqual(set(self.room.votes), {self.viewer.id})
        self.assertNotIn(keep.id, self.room.anarchy_by_id)
        self.assertNotIn(keep.ip, self.room.anarchy_by_ip)
        self.game.steps.clear()
        self.tick(10000)
        self.assertFalse(self.game.steps)

    def test_empty_and_paused_clear_bot_votes_without_wake_or_backlog(self):
        self.room.set_simulated_players(1)
        self.room.handle_vote(self.viewer, 'b', 1)
        self.tick(100)
        self.tick(104)
        self.assertEqual(len(self.room.votes), 2)
        self.room.viewers.clear()
        self.game.set_viewers(False)
        self.game.wake.clear()
        self.tick(108)
        self.assertEqual(set(self.room.votes), {self.viewer.id})
        self.assertFalse(self.game.viewers.is_set())
        self.assertFalse(self.game.wake.is_set())
        self.assertFalse(self.game.steps)
        self.room.viewers.add(self.viewer)
        self.game.admin_paused = True
        self.tick(10000)
        self.assertEqual(set(self.room.votes), {self.viewer.id})
        self.game.admin_paused = False
        self.tick(20000)
        self.assertEqual(set(self.room.votes), {self.viewer.id})
        self.tick(20004)
        self.assertEqual(len(self.room.votes), 2)

    def test_real_mode_voting_dedup_and_ip_caps_ignore_bots(self):
        self.room.set_simulated_players(50)
        self.room.handle_mode(self.viewer, 'democracy')
        for n in range(10):
            duplicate = Viewer(None, f'duplicate-ip-{n}')
            duplicate.id = self.viewer.id
            self.room.viewers.add(duplicate)
            self.room.handle_mode(duplicate, 'democracy')
            attacker = Viewer(None, 'shared-ip')
            attacker.id = f'attacker-{n}'
            self.room.viewers.add(attacker)
            self.room.handle_mode(attacker, 'anarchy')
            self.room.handle_vote(attacker, 'b', 1)
        self.tick(100)
        self.tick(101)
        self.assertEqual(self.room.update_mode(), .75)
        self.assertEqual(self.room.mode_voters, 4)
        self.assertEqual(self.room.tally()['b:1'], 3)
        self.assertTrue(all(p.id not in self.room.mode_votes for p in self.room.simulated_players))

    def test_anarchy_inputs_are_bounded_safe_and_respect_existing_limits(self):
        self.room.mode = 'anarchy'
        self.room.set_simulated_players(50)
        rng = random.Random(7)
        with patch('run.random.choice', side_effect=rng.choice):
            for i in range(1000):
                now = 100 + i * .25
                with patch('run.time.monotonic', return_value=now):
                    self.room.tick_simulated_players(now)
        self.assertEqual(len(self.game.steps), ANARCHY_QUEUE_CAP)
        self.assertEqual(self.room.total_inputs, 0)
        self.assertTrue(all(b in SIMULATED_BUTTONS for b, _, _ in self.game.steps))
        self.assertTrue(set(SIMULATED_BUTTONS).isdisjoint({'start', 'select', 'l', 'r'}))
        self.assertLessEqual(len(self.room.anarchy_by_id), 50)
        self.assertLessEqual(len(self.room.anarchy_by_ip), 50)
        # An input-only identity still uses the same rate/burst limits and drops notices.
        self.game.steps.clear()
        player = self.room.simulated_players[0]
        with patch('run.time.monotonic', return_value=1000):
            self.room.handle_vote(player, 'a', 1)
            self.room.handle_vote(player, 'a', 1)
        self.assertEqual(len(self.game.steps), 1)
        self.room.anarchy_hold_unlimited = True
        self.assertTrue(self.room.state_msg(None)['anarchyHoldUnlimited'])
        self.assertFalse(hasattr(player, 'queue'))

    def test_admin_input_count_skips_bots(self):
        self.room.mode = 'anarchy'
        self.room.set_simulated_players(1)
        bot = self.room.simulated_players[0]
        with patch('run.time.monotonic', return_value=100):
            self.room.handle_vote(bot, 'a', 1)
            self.room.handle_vote(self.viewer, 'b', 1)
        self.assertEqual(len(self.game.steps), 2)
        self.assertEqual(self.room.total_inputs, 1)
        # Democracy: a winner counts once if any real player backed it, never for bots alone.
        self.room.set_mode('democracy', lock=False)
        self.game.steps.clear()
        now = 200
        self.room.window_end = now
        self.room.handle_vote(bot, 'a', 1)
        self.room.handle_vote(self.viewer, 'a', 1)

        def run_window():
            start = now

            async def close_one_window(_):
                nonlocal now
                if now > start:
                    raise asyncio.CancelledError
                now += .25

            with patch('run.time.monotonic', side_effect=lambda: now), \
                    patch.object(self.room, 'tick_simulated_players'), \
                    patch('run.asyncio.sleep', side_effect=close_one_window):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(self.room.ticker_loop())

        run_window()
        self.assertEqual(self.room.total_inputs, 2)
        self.game.steps.clear()
        now = 300
        self.room.window_end = now
        self.room.handle_vote(bot, 'a', 1)
        run_window()
        self.assertEqual(len(self.game.steps), 1)
        self.assertEqual(self.room.total_inputs, 2)

    def drift_run(self, seed, lo, hi, seconds, step=1.0):
        """Tick a seeded room; return [(time, count)] at every count change."""
        random.seed(seed)
        self.room.set_simulated_range(lo, hi)
        changes, last, t = [], len(self.room.simulated_players), 1000.0
        with patch.object(self.room, 'handle_vote'):
            for _ in range(int(seconds / step)):
                t += step
                self.room.tick_simulated_players(t)
                n = len(self.room.simulated_players)
                self.assertTrue(lo <= n <= hi)
                if n != last:
                    changes.append((t, n - last))
                    last = n
        return changes

    def test_range_validation(self):
        for lo, hi in [(5, 4), (-1, 3), (0, 51), (True, 3), (1, 2.0), ('1', 2), (None, 2)]:
            with self.subTest(lo=lo, hi=hi), self.assertRaises(ValueError):
                self.room.set_simulated_range(lo, hi)
        self.assertEqual((self.room.simulated_min, self.room.simulated_max, len(self.room.simulated_players)), (0, 0, 0))

    def test_fixed_range_is_exact_and_never_drifts(self):
        self.assertEqual(self.drift_run(1, 7, 7, 3600), [])
        self.assertEqual(len(self.room.simulated_players), 7)

    def test_apply_keeps_count_inside_range_and_identities(self):
        self.room.set_simulated_players(10)
        ids = [p.id for p in self.room.simulated_players]
        self.room.set_simulated_range(5, 20)
        self.assertEqual([p.id for p in self.room.simulated_players], ids)
        for seed in range(20):
            random.seed(seed)
            self.room.set_simulated_range(30, 40)
            self.assertTrue(30 <= len(self.room.simulated_players) <= 40)
            self.room.set_simulated_range(0, 3)
            self.assertTrue(len(self.room.simulated_players) <= 3)

    def test_drift_stays_in_bounds_with_small_slow_steps(self):
        for seed in range(5):
            for lo, hi in [(0, 50), (10, 14), (0, 1), (20, 21)]:
                changes = self.drift_run(seed, lo, hi, 6 * 3600)
                self.assertTrue(changes, (seed, lo, hi))
                self.assertTrue(all(1 <= abs(d) <= 3 for _, d in changes))
                times = [t for t, _ in changes]
                self.assertTrue(all(b - a >= 15 for a, b in zip(times, times[1:])), (seed, lo, hi))
        random.seed(3)
        changes = self.drift_run(3, 0, 50, 6 * 3600)
        singles = sum(abs(d) == 1 for _, d in changes)
        self.assertTrue(0.7 < singles / len(changes) < 0.95)
        self.assertTrue(any(abs(d) >= 2 for _, d in changes))

    def test_drift_wanders_across_the_range(self):
        random.seed(11)
        self.room.set_simulated_range(0, 30)
        seen, t = set(), 1000.0
        with patch.object(self.room, 'handle_vote'):
            for _ in range(24 * 3600):
                t += 1
                self.room.tick_simulated_players(t)
                seen.add(len(self.room.simulated_players))
        self.assertGreater(max(seen) - min(seen), 15)

    def test_no_drift_without_viewers_or_while_paused_and_no_burst_on_resume(self):
        random.seed(5)
        self.room.set_simulated_range(0, 50)
        with patch.object(self.room, 'handle_vote'):
            self.room.tick_simulated_players(1000)
            start = len(self.room.simulated_players)
            self.room.viewers.clear()
            for t in range(1001, 20000, 7):
                self.room.tick_simulated_players(t)
            self.assertEqual(len(self.room.simulated_players), start)
            self.room.viewers.add(self.viewer)
            self.game.admin_paused = True
            for t in range(20001, 40000, 7):
                self.room.tick_simulated_players(t)
            self.assertEqual(len(self.room.simulated_players), start)
            self.game.admin_paused = False
            self.room.tick_simulated_players(40000)      # reschedules from now
            self.assertEqual(len(self.room.simulated_players), start)
            self.room.tick_simulated_players(40014.9)
            self.assertEqual(len(self.room.simulated_players), start)
            self.room.tick_simulated_players(40091)       # past the 90 s maximum gap
            self.assertLessEqual(abs(len(self.room.simulated_players) - start), 3)
            after = len(self.room.simulated_players)
            self.room.tick_simulated_players(40091.1)
            self.assertEqual(len(self.room.simulated_players), after)

    def test_drift_from_zero_floor_still_starts(self):
        changes = self.drift_run(2, 0, 4, 3600)
        self.assertTrue(changes)

    def test_departing_bots_drop_votes_and_stayers_keep_identity(self):
        random.seed(8)
        self.room.set_simulated_range(0, 50)
        self.room.resize_simulated_players(20)
        for player in self.room.simulated_players:
            self.room.handle_vote(player, 'a', 1)
        before = list(self.room.simulated_players)
        t = 1000.0
        with patch.object(self.room, 'handle_vote'):
            for _ in range(7200):
                t += 1
                self.room.tick_simulated_players(t)
                live = {p.id for p in self.room.simulated_players}
                self.assertFalse({i for i in self.room.votes if i not in live and i != self.viewer.id})
        left = [p for p in before if p not in self.room.simulated_players]
        self.assertTrue(left)
        self.assertTrue(all(p.id not in self.room.votes for p in left))
        stayers = [p for p in before if p in self.room.simulated_players]
        self.assertEqual(stayers, [p for p in self.room.simulated_players if p in before])

    def test_settings_restart_restore_and_invalid_or_legacy_counts_off(self):
        settings = Path(self.storage.name) / 'settings.json'
        for saved, expected in [({}, 0), ({'simulatedPlayers': 50}, 50),
                                ({'simulatedPlayers': 10, 'window': 'invalid', 'paused': True}, 0),
                                *[({'simulatedPlayers': invalid}, 0)
                                  for invalid in (True, 1.5, '4', -1, 51, None, [], {})]]:
            with self.subTest(saved=saved):
                settings.write_text(json.dumps(saved))
                room = Room(self.game, 1500)
                load_settings(room, self.game, str(settings))
                self.assertEqual(len(room.simulated_players), expected)
                self.assertFalse(room.viewers)
                self.assertIsNone(room.simulated_next_at)
                self.assertEqual((room.simulated_min, room.simulated_max), (expected, expected))

    def test_settings_range_restore_and_invalid_range_off(self):
        settings = Path(self.storage.name) / 'settings.json'
        for saved, expected in [({'simulatedMin': 5, 'simulatedMax': 12}, (5, 12)),
                                ({'simulatedMin': 5, 'simulatedMax': 12, 'simulatedPlayers': 9}, (5, 12)),
                                ({'simulatedMin': 0, 'simulatedMax': 50, 'simulatedPlayers': 25}, (0, 50)),
                                ({'simulatedMin': 5, 'simulatedMax': 12, 'simulatedPlayers': 30}, (5, 12)),
                                ({'simulatedMin': 0, 'simulatedMax': 50}, (0, 50)),
                                ({'simulatedMin': 9, 'simulatedMax': 3}, (0, 0)),
                                ({'simulatedMin': 9}, (0, 0)), ({'simulatedMax': 9}, (0, 9)),
                                ({'simulatedMin': True, 'simulatedMax': 3}, (0, 0)),
                                ({'simulatedMin': 1, 'simulatedMax': 51}, (0, 0))]:
            with self.subTest(saved=saved):
                settings.write_text(json.dumps(saved))
                room = Room(self.game, 1500)
                load_settings(room, self.game, str(settings))
                self.assertEqual((room.simulated_min, room.simulated_max), expected)
                self.assertTrue(expected[0] <= len(room.simulated_players) <= expected[1])
                if 'simulatedPlayers' in saved:  # the live count survives a restart, clamped
                    self.assertEqual(len(room.simulated_players),
                                     max(expected[0], min(expected[1], saved['simulatedPlayers'])))


class SimulatedAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.game = GameLoop(SaveCore(), self.storage.name, lambda *args: None)
        self.room = Room(self.game, 1500)
        self.settings = Path(self.storage.name) / 'settings.json'
        with patch.dict(os.environ, {'XP_ADMIN_HOST': 'admin.example.com'}):
            app = make_app(self.room, self.game, self.game.emu, 'local-test', '', str(self.settings))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.allowed = {'host': 'admin.example.com', 'cf-access-jwt-assertion': 'local-fixture',
                        'x-admin-token': 'local-test'}

    async def test_protected_config_gate_and_invalid_mixed_patch_is_atomic(self):
        for headers, status in [({'host': 'play.example.com', 'x-admin-token': 'local-test'}, 404),
                                ({'host': 'admin.example.com', 'x-admin-token': 'local-test'}, 404),
                                ({**self.allowed, 'x-admin-token': 'wrong'}, 403)]:
            response = await self.client.post('/admin/api/config', json={'simulatedPlayers': 5}, headers=headers)
            self.assertEqual(response.status, status)
        for invalid in (True, False, '4', 4.0, 1.5, -1, 51, None, [], {}):
            with self.subTest(invalid=invalid):
                response = await self.client.post('/admin/api/config', headers=self.allowed,
                                                 json={'window': 500, 'paused': True, 'theme': 'lime',
                                                       'anarchyHoldUnlimited': True, 'simulatedPlayers': invalid})
                self.assertEqual(response.status, 400)
                self.assertEqual(self.room.window_ms, 1500)
                self.assertEqual(self.room.theme, 'flame')
                self.assertFalse(self.room.anarchy_hold_unlimited)
                self.assertFalse(self.game.admin_paused)
                self.assertFalse(self.room.simulated_players)
                self.assertFalse(self.settings.exists())

    async def post(self, body):
        return await self.client.post('/admin/api/config', json=body, headers=self.allowed)

    async def test_range_api_validation_is_atomic_with_clear_message(self):
        bad = [{'simulatedMin': 5, 'simulatedMax': 4}, {'simulatedMin': -1, 'simulatedMax': 4},
               {'simulatedMin': 0, 'simulatedMax': 51}, {'simulatedMin': True, 'simulatedMax': 4},
               {'simulatedMin': 1, 'simulatedMax': 2.0}, {'simulatedMin': '1', 'simulatedMax': 2},
               {'simulatedMin': None, 'simulatedMax': 2}, {'simulatedMin': 1, 'simulatedMax': []},
               {'simulatedMin': 3}, {'simulatedMax': -1}, {'simulatedMin': 1, 'simulatedPlayers': 2}]
        await self.post({'simulatedMin': 2, 'simulatedMax': 2})
        self.settings.unlink()
        for body in bad:
            with self.subTest(body=body):
                response = await self.post({'window': 500, 'theme': 'lime', **body})
                self.assertEqual(response.status, 400)
                self.assertRegex(await response.text(), 'simulated')
                self.assertEqual((self.room.simulated_min, self.room.simulated_max, len(self.room.simulated_players)), (2, 2, 2))
                self.assertEqual((self.room.window_ms, self.room.theme), (1500, 'flame'))
                self.assertFalse(self.settings.exists())

    async def test_range_api_applies_persists_and_reports_live_count(self):
        response = await self.post({'simulatedMin': 4, 'simulatedMax': 9})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body['simulatedMin'], body['simulatedMax']), (4, 9))
        self.assertTrue(4 <= body['simulatedPlayers'] <= 9)
        self.assertEqual(body['simulatedPlayers'], len(self.room.simulated_players))
        self.assertEqual(self.room.state_msg(None)['simulatedPlayers'], body['simulatedPlayers'])
        saved = json.loads(self.settings.read_text())
        self.assertEqual((saved['simulatedMin'], saved['simulatedMax']), (4, 9))
        keep = list(self.room.simulated_players)
        await self.post({'simulatedMin': 0, 'simulatedMax': 50})
        self.assertEqual(self.room.simulated_players, keep)
        response = await self.post({'simulatedMax': 60})
        self.assertEqual(response.status, 400)
        response = await self.post({'simulatedMax': 3})            # min stays 0
        self.assertEqual((await response.json())['simulatedMax'], 3)
        self.assertTrue(len(self.room.simulated_players) <= 3)

    async def test_legacy_simulated_players_api_is_fixed_range(self):
        body = await (await self.post({'simulatedPlayers': 6})).json()
        self.assertEqual((body['simulatedMin'], body['simulatedMax'], body['simulatedPlayers']), (6, 6, 6))
        body = await (await self.post({'simulatedPlayers': 0})).json()
        self.assertEqual((body['simulatedMin'], body['simulatedMax'], body['simulatedPlayers']), (0, 0, 0))

    async def test_persist_status_hello_and_live_state_preserve_real_viewers(self):
        for count in (50, 3):
            response = await self.client.post('/admin/api/config', json={'simulatedPlayers': count}, headers=self.allowed)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['simulatedPlayers'], count)
        self.assertEqual(json.loads(self.settings.read_text())['simulatedPlayers'], 3)
        restored = Room(self.game, 1500)
        load_settings(restored, self.game, str(self.settings))
        self.assertEqual(len(restored.simulated_players), 3)
        async with self.client.ws_connect('/ws') as ws:
            hello = await ws.receive_json(timeout=1)
            self.assertEqual((hello['viewers'], hello['simulatedPlayers']), (1, 3))
            await ws.receive_bytes(timeout=1)
            ticker = asyncio.create_task(self.room.ticker_loop())
            try:
                state = await ws.receive_json(timeout=1)
                self.assertEqual(state['t'], 'state')
                self.assertEqual((state['viewers'], state['simulatedPlayers']), (1, 3))
                response = await self.client.get('/admin/api/status', headers=self.allowed)
                status = await response.json()
                self.assertEqual((status['viewers'], status['simulatedPlayers']), (1, 3))
                self.assertEqual(status['settings']['simulatedPlayers'], 3)
                response = await self.client.post('/admin/api/config', json={'simulatedPlayers': 0}, headers=self.allowed)
                self.assertEqual(response.status, 200)
                state = await ws.receive_json(timeout=1)
                self.assertEqual((state['viewers'], state['simulatedPlayers']), (1, 0))
            finally:
                ticker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await ticker
        self.assertEqual(json.loads(self.settings.read_text())['simulatedPlayers'], 0)

    async def test_admin_pause_immediately_drops_pending_bot_votes(self):
        self.room.set_simulated_players(1)
        player = self.room.simulated_players[0]
        self.room.handle_vote(player, 'a', 1)
        response = await self.client.post('/admin/api/config', json={'paused': True}, headers=self.allowed)
        self.assertEqual(response.status, 200)
        self.assertFalse(self.room.votes)


if __name__ == '__main__':
    unittest.main()
