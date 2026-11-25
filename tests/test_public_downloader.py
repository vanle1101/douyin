import ipaddress
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request

import public_downloader as pd


class PublicDownloaderBoundaryTests(unittest.TestCase):
    def test_only_expected_https_douyin_video_and_share_paths_are_accepted(self):
        for url in (
            'https://www.douyin.com/video/1234567890',
            'https://v.douyin.com/AbC_123/',
            'https://www.iesdouyin.com/share/video/1234567890?from=share',
        ):
            self.assertEqual(pd.validate_douyin_url(url), url)
        for url in (
            'http://www.douyin.com/video/1234567890',
            'https://www.douyin.com.attacker.invalid/video/1234567890',
            'https://www.douyin.com@127.0.0.1/video/1234567890',
            'https://www.douyin.com./video/1234567890',
            'https://www.douyin.com:444/video/1234567890',
            'https://127.0.0.1/latest/meta-data/',
            'https://www.douyin.com/redirect?url=https://127.0.0.1/',
            'https://www.douyin.com/api/v1/aweme/detail/',
        ):
            with self.subTest(url=url), self.assertRaises(pd.PublicDownloadError):
                pd.validate_douyin_url(url)

    def test_short_link_redirect_must_stay_https_and_inside_douyin_hosts(self):
        handler = pd._DouyinRedirectHandler()
        request = Request('https://v.douyin.com/AbC123/')
        allowed = handler.redirect_request(request, None, 302, 'Found', {}, 'https://www.douyin.com/video/1234567890')
        self.assertEqual(allowed.full_url, 'https://www.douyin.com/video/1234567890')
        for target in (
            'http://www.douyin.com/video/1234567890',
            'https://127.0.0.1/latest/meta-data/',
            'https://169.254.169.254/latest/meta-data/',
            'https://v.douyin.com.attacker.invalid/video/1234567890',
            'https://evil.example/video/1234567890',
            'https://www.douyin.com@127.0.0.1/video/1234567890',
        ):
            with self.subTest(target=target), self.assertRaises(pd.PublicDownloadError):
                pd._DouyinRedirectHandler().redirect_request(request, None, 302, 'Found', {}, target)

    def test_downloader_egress_hostnames_are_allowlisted(self):
        for host in ('www.douyin.com', 'api.douyin.com', 'v3-web.douyinvod.com', 'aweme.snssdk.com', 'p3-sign.douyinpic.com'):
            self.assertEqual(pd.validate_egress_host(host), host)
        for host in ('example.com', 'www.douyin.com.attacker.invalid', '127.0.0.1', '169.254.169.254', 'evil.bytecdn.cn.attacker.invalid'):
            with self.subTest(host=host), self.assertRaises(pd.PublicDownloadError):
                pd.validate_egress_host(host)

    def test_dns_must_be_entirely_public_and_is_rechecked_per_connection(self):
        def answers(*addresses):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443)) for address in addresses]

        public = pd.resolve_public_addresses('www.douyin.com', resolver=lambda *_args: answers('8.8.8.8'))
        self.assertEqual(public[0][3], ('8.8.8.8', 443))
        for private_address in ('127.0.0.1', '10.2.3.4', '169.254.169.254', '192.168.1.1', '100.64.0.1'):
            with self.subTest(address=private_address), self.assertRaises(pd.PublicDownloadError):
                pd.resolve_public_addresses('www.douyin.com', resolver=lambda *_args, ip=private_address: answers(ip))
        with self.assertRaises(pd.PublicDownloadError):
            pd.resolve_public_addresses('www.douyin.com', resolver=lambda *_args: answers('8.8.8.8', '169.254.169.254'))
        with self.assertRaises(pd.PublicDownloadError):
            pd.resolve_public_addresses('www.douyin.com', resolver=lambda *_args: answers('::ffff:127.0.0.1'))

        resolutions = iter((answers('8.8.8.8'), answers('127.0.0.1')))
        resolver = lambda *_args: next(resolutions)
        pd.resolve_public_addresses('www.douyin.com', resolver=resolver)
        with self.assertRaises(pd.PublicDownloadError):
            pd.resolve_public_addresses('www.douyin.com', resolver=resolver)

    def test_connection_uses_the_checked_ip_without_second_dns_lookup(self):
        class FakeSocket:
            def __init__(self, *_args):
                self.connected_to = None
            def settimeout(self, _timeout):
                pass
            def connect(self, address):
                self.connected_to = address
            def close(self):
                pass

        fake = FakeSocket()
        with mock.patch.object(pd.socket, 'socket', return_value=fake):
            result = pd.connect_public('www.douyin.com', resolver=lambda *_args: [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443)),
            ])
        self.assertIs(result, fake)
        self.assertEqual(fake.connected_to, ('8.8.8.8', 443))

    def test_video_metadata_limits_fail_before_yt_dlp_download(self):
        class FakeYDL:
            options = None
            processed = False
            def __init__(self, options):
                type(self).options = options
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def extract_info(self, *_args, **_kwargs):
                return {'id': '123', 'duration': 7201, 'filesize': 1000}
            def process_ie_result(self, *_args, **_kwargs):
                type(self).processed = True

        class FakeDownloadError(Exception):
            pass

        package = types.ModuleType('yt_dlp')
        package.YoutubeDL = FakeYDL
        utils = types.ModuleType('yt_dlp.utils')
        utils.DownloadError = FakeDownloadError

        class Proxy:
            def __init__(self, *_args, **_kwargs):
                pass
            def __enter__(self):
                return 'http://test:test@127.0.0.1:34567'
            def __exit__(self, *_args):
                return False
            def set_runtime_deadline(self, _seconds):
                pass

        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.dict(sys.modules, {'yt_dlp': package, 'yt_dlp.utils': utils}), \
             mock.patch.object(pd, 'SafeDouyinProxy', Proxy), \
             mock.patch.object(pd, 'resolve_douyin_video_url', return_value='https://www.douyin.com/video/123'):
            with self.assertRaises(pd.PublicDownloadError) as caught:
                pd.download_douyin('https://www.douyin.com/video/123', Path(temporary), 10_000_000)
        self.assertEqual(caught.exception.code, 'video_duration_limit')
        self.assertFalse(FakeYDL.processed)
        self.assertTrue(FakeYDL.options['ignoreconfig'])
        self.assertIsNone(FakeYDL.options['cookiefile'])
        self.assertFalse(FakeYDL.options['usenetrc'])

    def test_valid_metadata_starts_a_bounded_download_phase(self):
        class FakeYDL:
            options = None
            processed = False
            def __init__(self, options):
                type(self).options = options
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def extract_info(self, *_args, **_kwargs):
                return {'id': '123', 'duration': 60, 'filesize': 1000}
            def process_ie_result(self, _info, download=False):
                type(self).processed = download
                Path(self.options['outtmpl'].replace('%(ext)s', 'mp4')).write_bytes(b'x' * 128 * 1024)
                return _info

        class FakeDownloadError(Exception):
            pass

        package = types.ModuleType('yt_dlp')
        package.YoutubeDL = FakeYDL
        utils = types.ModuleType('yt_dlp.utils')
        utils.DownloadError = FakeDownloadError

        class Proxy:
            deadline_seconds = None
            def __init__(self, *_args, **_kwargs):
                pass
            def __enter__(self):
                return 'http://test:test@127.0.0.1:34567'
            def __exit__(self, *_args):
                return False
            def set_runtime_deadline(self, seconds):
                type(self).deadline_seconds = seconds

        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.dict(sys.modules, {'yt_dlp': package, 'yt_dlp.utils': utils}), \
             mock.patch.object(pd, 'SafeDouyinProxy', Proxy), \
             mock.patch.object(pd, 'resolve_douyin_video_url', return_value='https://www.douyin.com/video/123'):
            result = pd.download_douyin('https://www.douyin.com/video/123', Path(temporary), 1_000_000)
            self.assertEqual(result.stat().st_size, 128 * 1024)
        self.assertTrue(FakeYDL.processed)
        self.assertEqual(Proxy.deadline_seconds, pd.MAX_DOWNLOAD_SECONDS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
