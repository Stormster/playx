"""Per-deployment page settings (X handle, footer link) and the admin hostname lock; fake core only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from run import GameLoop, Room, make_app


class FakeCore:
    fps = 60
    sample_rate = 32768
    frame = np.zeros((160, 240), dtype=np.uint16)
    pressed = set()

    def run_frame(self):
        pass


class PageSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def page(self, path, env, host='play.example.com'):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, env, clear=False):
            for key in ('XP_X_HANDLE', 'XP_FOOTER_TEXT', 'XP_FOOTER_URL', 'XP_ADMIN_HOST'):
                if key not in env:
                    os.environ.pop(key, None)
            game = GameLoop(FakeCore(), data, lambda *args: None)
            app = make_app(Room(game, 1500), game, game.emu, 'local-test', 'https://play.example.com',
                           str(Path(data) / 'settings.json'))
            async with TestClient(TestServer(app)) as client:
                response = await client.get(path, headers={'host': host, 'x-admin-token': 'local-test'})
                return response.status, await response.text()

    async def test_unset_settings_leave_no_placeholders_or_handle(self):
        for path in ('/', '/embed', '/play'):
            status, html = await self.page(path, {})
            self.assertEqual(status, 200)
            self.assertNotIn('__', html)
            self.assertNotIn('twitter:site', html)

    async def test_handle_and_footer_are_filled_in_and_escaped(self):
        status, html = await self.page('/', {'XP_X_HANDLE': '@someone', 'XP_FOOTER_TEXT': 'a"<b>.dev',
                                             'XP_FOOTER_URL': 'https://example.com/?a=1&b=2'})
        self.assertEqual(status, 200)
        self.assertIn('<meta name="twitter:site" content="@someone">', html)
        self.assertIn('data-footer-text="a&quot;&lt;b&gt;.dev"', html)
        self.assertIn('data-footer-url="https://example.com/?a=1&amp;b=2"', html)
        self.assertIn('content="https://play.example.com/embed"', html)

    async def test_admin_is_token_only_unless_a_host_is_set(self):
        status, _ = await self.page('/admin/api/status', {})
        self.assertEqual(status, 200)
        status, _ = await self.page('/admin/api/status', {'XP_ADMIN_HOST': 'admin.example.com'})
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
