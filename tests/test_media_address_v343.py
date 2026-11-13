"""V3.4.3 media address discovery; all network operations are mocked."""
import socket
import unittest
from unittest.mock import patch

from adbtv.media_address import resolve_media_host


class MediaAddressTests(unittest.TestCase):
    def route(self, address='10.23.4.8'):
        mocked = patch('adbtv.media_address.socket.socket')
        factory = mocked.start()
        self.addCleanup(mocked.stop)
        probe = factory.return_value.__enter__.return_value
        probe.getsockname.return_value = (address, 48000)
        return factory, probe

    def test_advertise_host_wins_without_network_discovery(self):
        with patch('adbtv.media_address.socket.socket') as factory:
            self.assertEqual(resolve_media_host(
                {'adb': {'host': '10.23.4.20'}}, host='10.9.0.8',
                advertise_host=' 10.23.4.8 '), '10.23.4.8')
        factory.assert_not_called()

    def test_manual_host_without_config(self):
        self.assertEqual(resolve_media_host(host='10.23.4.8'), '10.23.4.8')

    def test_manual_hostname_resolves_ipv4(self):
        with patch('adbtv.media_address.socket.getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('10.23.4.8', 9))
        ]) as lookup:
            self.assertEqual(resolve_media_host(host='media.example'), '10.23.4.8')
        self.assertEqual(lookup.call_args.kwargs['family'], socket.AF_INET)

    def test_invalid_manual_address_does_not_fall_back(self):
        for value in ('0.0.0.0', '::', '*', '127.0.0.1', 'localhost',
                      '255.255.255.255', '224.0.0.1', '169.254.1.8', '::1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_media_host({'adb': {'host': '10.23.4.20'}},
                                   advertise_host=value)

    def test_loopback_host_rejected_for_tv_urls(self):
        for value in ('localhost', 'LOCALHOST.', '127.0.0.1', '127.20.3.4'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'loopback'):
                resolve_media_host(host=value)

    def test_explicit_loopback_allowed_for_internal_use(self):
        for value in ('localhost', '127.0.0.1'):
            with self.subTest(value=value):
                self.assertEqual(resolve_media_host(host=value, allow_loopback=True),
                                 '127.0.0.1')
        self.assertEqual(resolve_media_host(advertise_host='127.0.0.1',
                                           allow_loopback=True), '127.0.0.1')

    def test_allow_loopback_does_not_allow_wildcard_advertisement(self):
        with self.assertRaises(ValueError):
            resolve_media_host(advertise_host='0.0.0.0', allow_loopback=True)

    def test_configured_tv_host_selects_local_route(self):
        factory, probe = self.route()
        self.assertEqual(resolve_media_host({'adb': {'host': '10.23.4.20'}}),
                         '10.23.4.8')
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect.assert_called_once_with(('10.23.4.20', 9))
        factory.return_value.__exit__.assert_called_once()

    def test_host_preferred_over_serial(self):
        _, probe = self.route()
        resolve_media_host({'adb': {'host': '10.23.4.20', 'serial': '10.9.0.20:5555'}})
        probe.connect.assert_called_once_with(('10.23.4.20', 9))

    def test_network_serial_selects_tv_route(self):
        _, probe = self.route()
        self.assertEqual(resolve_media_host({'adb': {'serial': '172.22.4.20:5555'}}),
                         '10.23.4.8')
        probe.connect.assert_called_once_with(('172.22.4.20', 9))

    def test_network_serial_hostname(self):
        _, probe = self.route()
        with patch('adbtv.media_address.socket.getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('10.23.4.20', 9))
        ]):
            resolve_media_host({'adb': {'serial': 'tv.example:5555'}})
        probe.connect.assert_called_once_with(('10.23.4.20', 9))

    def test_wildcard_bind_host_triggers_discovery(self):
        for value in ('', '0.0.0.0', '::', '*', '[::]'):
            with self.subTest(value=value):
                self.route()
                self.assertEqual(resolve_media_host({'adb': {'host': '10.23.4.20'}},
                                                    host=value), '10.23.4.8')

    def test_no_config_or_usb_serial_fails_without_network_probe(self):
        with patch('adbtv.media_address.socket.socket') as factory:
            for cfg in (None, {}, {'adb': {}}, {'adb': {'serial': 'USB1234'}},
                        {'adb': {'serial': 'emulator-5554'}}):
                with self.subTest(cfg=cfg), self.assertRaisesRegex(ValueError, 'advertise_host'):
                    resolve_media_host(cfg)
        factory.assert_not_called()

    def test_offline_route_raises_clear_error(self):
        _, probe = self.route()
        probe.connect.side_effect = OSError('Network is unreachable')
        with self.assertRaisesRegex(ValueError, 'advertise_host'):
            resolve_media_host({'adb': {'host': '10.23.4.20'}})

    def test_socket_creation_failure_is_value_error(self):
        with patch('adbtv.media_address.socket.socket', side_effect=OSError('offline')):
            with self.assertRaisesRegex(ValueError, 'advertise_host'):
                resolve_media_host({'adb': {'host': '10.23.4.20'}})

    def test_unusable_discovered_addresses_always_rejected(self):
        for value in ('127.0.0.1', '0.0.0.0', '224.1.2.3', '169.254.8.1', '::1'):
            for allowed in (False, True):
                with self.subTest(value=value, allow_loopback=allowed):
                    self.route(value)
                    with self.assertRaises(ValueError):
                        resolve_media_host({'adb': {'host': '10.23.4.20'}},
                                           allow_loopback=allowed)

    def test_dns_failure_is_value_error(self):
        with patch('adbtv.media_address.socket.getaddrinfo',
                   side_effect=socket.gaierror('unknown host')):
            with self.assertRaisesRegex(ValueError, 'IPv4'):
                resolve_media_host(host='missing.example')

    def test_dns_loopback_answer_rejected(self):
        with patch('adbtv.media_address.socket.getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('127.0.0.1', 9))
        ]):
            with self.assertRaises(ValueError):
                resolve_media_host(advertise_host='media.example')

    def test_explicit_ipv6_rejected_for_ipv4_server(self):
        with self.assertRaisesRegex(ValueError, 'IPv4'):
            resolve_media_host(host='2001:db8::8')


if __name__ == '__main__':
    unittest.main()
