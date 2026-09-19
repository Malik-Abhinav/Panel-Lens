from unittest.mock import patch, MagicMock
import browser_bridge
import http_server


def test_bridge_starts_once_and_stops():
    server = MagicMock()
    server.server_address = ('127.0.0.1', 8765)
    browser_bridge.stop()
    with patch.object(http_server, 'ThreadingHTTPServer', return_value=server) as factory:
        assert browser_bridge.start()['ready']
        assert browser_bridge.start()['ready']
        factory.assert_called_once_with(('127.0.0.1', 8765), http_server.PanelLensRequestHandler)
        browser_bridge.stop()
    server.shutdown.assert_called_once()
    assert not browser_bridge.status()['ready']


def test_port_conflict_is_actionable():
    browser_bridge.stop()
    with patch.object(http_server, 'ThreadingHTTPServer', side_effect=OSError('Address in use')):
        state = browser_bridge.start()
    assert state['code'] == 'bridge_port_in_use'
    assert not state['ready']


def test_reject_malformed_extension_origins():
    assert not http_server.origin_allowed('chrome-extension://')
    assert not http_server.origin_allowed('chrome-extension://example.com')
    assert not http_server.origin_allowed('https://example.com')
    assert http_server.origin_allowed('chrome-extension://' + 'a' * 32)
