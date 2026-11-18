"""Offline examples of the shipped GitHub release detector."""
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus import gui_state, release_updates
from agentbus.web_bridge import WebBridge


def release(tag='v1.10.0', **fields):
    data = {'tag_name': tag, 'draft': False, 'prerelease': False,
            'assets': [{'name': release_updates.WINDOWS_ASSET, 'state': 'uploaded', 'size': 42}]}
    data.update(fields)
    return data


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='release-updates-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / 'preferences.json'
        preferences = patch.object(gui_state, 'state_path', return_value=self.state)
        preferences.start()
        self.addCleanup(preferences.stop)

    def test_only_newer_stable_release_with_windows_package_is_announced(self):
        check = release_updates.check_for_update
        self.assertEqual(check('agentbus-lab/AgentBus', '1.2.0', fetch=lambda _: release()),
                         {'status': 'update_available', 'current_version': '1.2.0',
                          'version': '1.10.0'})
        for tag in ('v1.2.0', 'v1.1.9', 'v0.8.1'):
            self.assertEqual(check('agentbus-lab/AgentBus', '1.2.0',
                                   fetch=lambda _: release(tag))['status'], 'current')
        for row in (release(prerelease=True), release(draft=True),
                    release('v1.10.0-rc.1')):
            self.assertEqual(check('agentbus-lab/AgentBus', '1.2.0',
                                   fetch=lambda _: row)['status'], 'no_release')
        self.assertEqual(check('agentbus-lab/AgentBus', '1.2.0',
                               fetch=lambda _: release(assets=[]))['status'], 'no_asset')

    def test_unconfigured_and_network_errors_do_not_claim_up_to_date(self):
        for repo in ('', 'https://example.org/x', 'ok/../no', 'owner/repo?token=demo'):
            with patch.object(release_updates, '_fetch_latest') as fetch:
                self.assertEqual(release_updates.check_for_update(repo, '1.2.0')['status'],
                                 'not_configured')
                fetch.assert_not_called()
        for exc, status in ((HTTPError('https://api.github.com', 404, 'missing', {}, None), 'no_release'),
                            (HTTPError('https://api.github.com', 403, 'limited', {}, None), 'unavailable'),
                            (URLError('offline'), 'unavailable'),
                            (http.client.IncompleteRead(b'not complete'), 'unavailable')):
            def fail(_repo):
                raise exc
            self.assertEqual(release_updates.check_for_update('agentbus-lab/AgentBus', '1.2.0',
                                                               fetch=fail)['status'], status)

    def test_http_fetch_uses_only_fixed_unauthenticated_metadata_endpoint(self):
        body = io.BytesIO(json.dumps(release()).encode('utf-8'))
        with patch.object(release_updates, 'build_opener') as opener:
            opener.return_value.open.return_value = body
            result = release_updates.check_for_update('agentbus-lab/AgentBus', '1.2.0')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,
                             'https://api.github.com/repos/agentbus-lab/AgentBus/releases/latest')
            self.assertEqual(request.get_method(), 'GET')
            self.assertNotIn('authorization', {k.lower() for k, _ in request.header_items()})
            self.assertIsNone(request.data)
        self.assertEqual(result['status'], 'update_available')

    def test_over_limit_response_cannot_be_used(self):
        with patch.object(release_updates, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(
                b'x' * (release_updates._MAX_RESPONSE_BYTES + 1))
            result = release_updates.check_for_update('agentbus-lab/AgentBus', '1.2.0')
        self.assertEqual(result['status'], 'unavailable')

    def test_opt_in_and_daily_limit_survive_new_bridge_instances(self):
        with patch.object(release_updates, 'GITHUB_RELEASE_REPOSITORY', 'agentbus-lab/AgentBus'), \
             patch.object(release_updates, '_fetch_latest', return_value=release()) as fetch:
            first = WebBridge(self.root)
            self.assertEqual(first.call('check_release', {'automatic': True})['data']['status'], 'disabled')
            self.assertFalse(self.state.exists())
            self.assertTrue(first.call('set_release_preferences', {'automatic': True})['ok'])
            self.assertEqual(first.call('check_release', {'automatic': True})['data']['status'],
                             'update_available')
            self.assertEqual(WebBridge(self.root).call('check_release', {'automatic': True})['data']['status'],
                             'deferred')
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(first.call('check_release', {'automatic': False})['data']['status'],
                             'update_available')
            self.assertEqual(fetch.call_count, 2)

    def test_only_verified_release_page_is_opened_on_request(self):
        with patch.object(release_updates, 'GITHUB_RELEASE_REPOSITORY', 'agentbus-lab/AgentBus'), \
             patch.object(release_updates, '_fetch_latest', return_value=release(
                 html_url='https://example.org/not-used')) as fetch, \
             patch.object(release_updates.webbrowser, 'open', return_value=True) as browser:
            bridge = WebBridge(self.root)
            self.assertFalse(bridge.call('open_release_page', {})['ok'])
            self.assertFalse(bridge.call('check_release', {'repository': 'another/project'})['ok'])
            bridge.call('check_release', {'automatic': False})
            fetch.assert_called_once_with('agentbus-lab/AgentBus')
            browser.assert_not_called()
            self.assertFalse(bridge.call('open_release_page', {'url': 'https://example.org'})['ok'])
            self.assertTrue(bridge.call('open_release_page', {})['ok'])
            browser.assert_called_once_with('https://github.com/agentbus-lab/AgentBus/releases/tag/v1.10.0')

    def test_network_check_cannot_hold_workbench_lock(self):
        def fetch(_):
            self.assertTrue(bridge._lock.acquire(blocking=False))
            bridge._lock.release()
            return release()
        # A separate thread checks the lock, because the workbench lock supports reentry.
        import concurrent.futures
        def inspect(_):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(fetch, None).result(timeout=2)
        with patch.object(release_updates, 'GITHUB_RELEASE_REPOSITORY', 'agentbus-lab/AgentBus'), \
             patch.object(release_updates, '_fetch_latest', side_effect=inspect):
            bridge = WebBridge(self.root)
            self.assertEqual(bridge.call('check_release', {})['data']['status'], 'update_available')


if __name__ == '__main__':
    unittest.main()
