"""Public-only Douyin downloader with a DNS-pinning HTTPS CONNECT proxy.

Only callers that already validated a Douyin URL should use this module. Every
outbound TLS connection is resolved and checked in the proxy, immediately
before the socket is connected. Redirects use a fresh CONNECT and are checked
the same way. The downloader never reads browser cookies or yt-dlp config.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import re
import secrets
import select
import shutil
import socket
import socketserver
import urllib.error
import urllib.parse
import urllib.request
import threading
import time
import math
from pathlib import Path
from urllib.parse import urlsplit


DOUYIN_HOSTS = frozenset({
    'douyin.com', 'www.douyin.com', 'v.douyin.com',
    'iesdouyin.com', 'www.iesdouyin.com',
})
DOUYIN_EGRESS_ROOTS = frozenset({
    'douyin.com', 'iesdouyin.com', 'douyinvod.com', 'snssdk.com', 'pstatp.com',
    'bytecdn.cn', 'bytecdn.com', 'douyinpic.com', 'byteimg.com',
})
SAFE_MEDIA_EXTENSIONS = frozenset({'.mp4', '.mov', '.mkv', '.webm'})
MAX_URL_LENGTH = 2048
CONNECT_TIMEOUT_SECONDS = 15
PROXY_IDLE_TIMEOUT_SECONDS = 60
MAX_PROXY_HEADER_BYTES = 16 * 1024
MAX_REDIRECTS = 8
MAX_DOWNLOAD_SECONDS = 2 * 60 * 60
MAX_METADATA_SECONDS = 5 * 60
DOUYIN_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'

_BLOCKED_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
    '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
    '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4',
    '::/128', '::1/128', '64:ff9b::/96', '64:ff9b:1::/48', '100::/64',
    '2001::/23', '2001:db8::/32', '2002::/16', 'fc00::/7', 'fe80::/10', 'ff00::/8',
))


class PublicDownloadError(Exception):
    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


def validate_douyin_url(value: object) -> str:
    raw = str(value or '')
    if not raw or len(raw) > MAX_URL_LENGTH or raw != raw.strip():
        raise PublicDownloadError('invalid_douyin_url', 'Dán một link Douyin HTTPS hợp lệ.')
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in raw):
        raise PublicDownloadError('invalid_douyin_url', 'Link Douyin không hợp lệ.')
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ''
        port = parsed.port
    except ValueError:
        raise PublicDownloadError('invalid_douyin_url', 'Link Douyin không hợp lệ.')
    if (
        parsed.scheme.lower() != 'https'
        or not host
        or host != host.lower()
        or host.endswith('.')
        or '@' in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or host not in DOUYIN_HOSTS
    ):
        raise PublicDownloadError('invalid_douyin_url', 'Chỉ nhận link HTTPS từ hostname Douyin được hỗ trợ.')
    if host in {'douyin.com', 'www.douyin.com'}:
        allowed_path = re.fullmatch(r'/(?:video/\d+|share/video/\d+)/?', parsed.path)
    else:
        allowed_path = re.fullmatch(r'/(?:[A-Za-z0-9_-]{3,128}|share/video/\d+)/?', parsed.path)
    if not allowed_path:
        raise PublicDownloadError('invalid_douyin_url', 'Link không trỏ trực tiếp tới video hoặc link chia sẻ Douyin.')
    return raw


def validate_douyin_redirect(value: object) -> str:
    """Validate every webpage redirect while resolving a Douyin share URL."""
    raw = str(value or '')
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ''
        port = parsed.port
    except ValueError:
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng bị từ chối vì URL không hợp lệ.')
    if (
        parsed.scheme.lower() != 'https' or not host or host != host.lower()
        or host.endswith('.') or '@' in parsed.netloc or parsed.username is not None
        or parsed.password is not None or port not in (None, 443) or parsed.fragment
        or host not in DOUYIN_HOSTS or not parsed.path
    ):
        raise PublicDownloadError('unsafe_redirect', 'Chuyển hướng ra ngoài hostname Douyin HTTPS được phép đã bị chặn.')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _checked_ip(str(literal))
    return raw


def _normal_host(host: str) -> str:
    host = str(host or '').strip()
    if not host or host.endswith('.') or '%' in host or any(ord(c) < 0x21 for c in host):
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng bị từ chối vì địa chỉ đích không an toàn.')
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        ascii_host = host.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng bị từ chối vì hostname không hợp lệ.')
    if len(ascii_host) > 253 or not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', ascii_host):
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng bị từ chối vì hostname không hợp lệ.')
    if ascii_host == 'localhost' or ascii_host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng tới mạng nội bộ đã bị chặn.')
    return ascii_host


def validate_egress_host(host: str) -> str:
    normalized = _normal_host(host)
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        if any(normalized == root or normalized.endswith('.' + root) for root in DOUYIN_EGRESS_ROOTS):
            return normalized
    raise PublicDownloadError('unsafe_redirect', 'Hostname tải video không thuộc các miền Douyin được cho phép.')


def _checked_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        ip = ipaddress.ip_address(str(address).split('%', 1)[0])
    except ValueError:
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng bị từ chối vì DNS trả địa chỉ không hợp lệ.')
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or any(ip.version == network.version and ip in network for network in _BLOCKED_NETWORKS):
        raise PublicDownloadError('unsafe_redirect', 'Một chuyển hướng tới mạng nội bộ hoặc địa chỉ metadata đã bị chặn.')
    return ip


def resolve_public_addresses(host: str, port: int = 443, resolver=None):
    """Resolve now, reject any non-public answer, and return pinned socket tuples."""
    normalized = validate_egress_host(host)
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        ip = _checked_ip(str(literal))
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sockaddr = (str(ip), port, 0, 0) if ip.version == 6 else (str(ip), port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, sockaddr)]
    resolver = resolver or socket.getaddrinfo
    try:
        answers = resolver(normalized, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        raise PublicDownloadError('dns_resolution_failed', 'Không phân giải được máy chủ của link hoặc chuyển hướng.')
    if not answers:
        raise PublicDownloadError('dns_resolution_failed', 'Không phân giải được máy chủ của link hoặc chuyển hướng.')
    pinned = []
    seen = set()
    for family, socktype, proto, _canonname, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise PublicDownloadError('unsafe_redirect', 'DNS trả loại địa chỉ không được hỗ trợ.')
        ip = _checked_ip(sockaddr[0])
        normalized_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        pinned_sockaddr = (str(ip), port, 0, 0) if ip.version == 6 else (str(ip), port)
        key = (normalized_family, pinned_sockaddr)
        if key not in seen:
            pinned.append((normalized_family, socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, pinned_sockaddr))
            seen.add(key)
    return pinned


def connect_public(host: str, port: int = 443, resolver=None, timeout: float = CONNECT_TIMEOUT_SECONDS):
    """Connect to one checked DNS answer without resolving the name a second time."""
    last_error = None
    for family, socktype, proto, sockaddr in resolve_public_addresses(host, port, resolver):
        upstream = socket.socket(family, socktype, proto)
        upstream.settimeout(timeout)
        try:
            upstream.connect(sockaddr)
            upstream.settimeout(PROXY_IDLE_TIMEOUT_SECONDS)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    raise PublicDownloadError('outbound_connect_failed', 'Không kết nối được tới máy chủ video công khai.') from last_error


class _ConnectProxyHandler(socketserver.BaseRequestHandler):
    def _reply(self, status: int, text: str) -> None:
        try:
            self.request.sendall(f'HTTP/1.1 {status} {text}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'.encode('ascii'))
        except OSError:
            pass

    def handle(self):
        client = self.request
        client.settimeout(CONNECT_TIMEOUT_SECONDS)
        buffer = bytearray()
        while b'\r\n\r\n' not in buffer and len(buffer) <= MAX_PROXY_HEADER_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                return
            buffer.extend(chunk)
        if len(buffer) > MAX_PROXY_HEADER_BYTES or b'\r\n\r\n' not in buffer:
            self._reply(431, 'Request Header Fields Too Large')
            return
        raw_headers, _separator, initial_data = bytes(buffer).partition(b'\r\n\r\n')
        try:
            lines = raw_headers.decode('iso-8859-1').split('\r\n')
            method, authority, version = lines[0].split(' ', 2)
        except (UnicodeError, ValueError, IndexError):
            self._reply(400, 'Bad Request')
            return
        if method.upper() != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1'):
            self._reply(403, 'HTTPS CONNECT Only')
            return
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(':')
            if sep:
                headers[name.strip().lower()] = value.strip()
        expected = 'Basic ' + self.server.auth_value
        if not hmac.compare_digest(headers.get('proxy-authorization', ''), expected):
            self._reply(407, 'Proxy Authentication Required')
            return
        try:
            if authority.startswith('['):
                end = authority.find(']')
                if end < 0 or authority[end + 1:] != ':443':
                    raise ValueError('bad authority')
                host = authority[1:end]
            else:
                host, raw_port = authority.rsplit(':', 1)
                if raw_port != '443' or ':' in host:
                    raise ValueError('bad authority')
            host = validate_egress_host(host)
        except (ValueError, PublicDownloadError):
            self._reply(403, 'Destination Rejected')
            return
        try:
            upstream = connect_public(host, 443, self.server.resolver)
        except PublicDownloadError:
            self._reply(403, 'Destination Rejected')
            return
        try:
            client.sendall(b'HTTP/1.1 200 Connection Established\r\nProxy-Agent: DouyinStudio\r\n\r\n')
            if initial_data:
                upstream.sendall(initial_data)
            peers = (client, upstream)
            last_activity = time.monotonic()
            while True:
                try:
                    readable, _writable, _exceptional = select.select(peers, (), peers, 1.0)
                except (OSError, ValueError):
                    return
                if not readable:
                    if time.monotonic() - last_activity > PROXY_IDLE_TIMEOUT_SECONDS:
                        return
                    if time.monotonic() > self.server.deadline:
                        return
                    continue
                for source in readable:
                    if time.monotonic() > self.server.deadline:
                        return
                    try:
                        data = source.recv(64 * 1024)
                    except OSError:
                        return
                    if not data:
                        return
                    last_activity = time.monotonic()
                    if source is upstream and not self.server.count_download_bytes(len(data)):
                        return
                    destination = upstream if source is client else client
                    try:
                        destination.sendall(data)
                    except OSError:
                        return
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class _ConnectProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, max_transfer_bytes: int, resolver=None, max_runtime_seconds: int = MAX_DOWNLOAD_SECONDS + MAX_METADATA_SECONDS):
        self.secret = secrets.token_urlsafe(24)
        self.auth_value = base64.b64encode(f'{self.secret}:{self.secret}'.encode()).decode('ascii')
        self.max_transfer_bytes = max(1, int(max_transfer_bytes))
        self.transferred = 0
        self._transfer_lock = threading.Lock()
        self.resolver = resolver
        self.deadline = time.monotonic() + max(1, int(max_runtime_seconds))
        super().__init__(('127.0.0.1', 0), _ConnectProxyHandler, bind_and_activate=True)

    def count_download_bytes(self, amount: int) -> bool:
        with self._transfer_lock:
            self.transferred += amount
            return self.transferred <= self.max_transfer_bytes


class SafeDouyinProxy:
    def __init__(self, max_file_bytes: int, resolver=None, max_runtime_seconds: int = MAX_METADATA_SECONDS):
        allowance = max(16 * 1024 * 1024, int(max_file_bytes * 0.02))
        self.server = _ConnectProxyServer(int(max_file_bytes) + allowance, resolver, max_runtime_seconds)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.server.server_address
        return f'http://{self.server.secret}:{self.server.secret}@{host}:{port}'

    def __exit__(self, _type, _value, _traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def set_runtime_deadline(self, seconds: int) -> None:
        self.server.deadline = time.monotonic() + max(1, int(seconds))


class _QuietLogger:
    def debug(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass


class _DouyinRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.redirect_count = 0

    def redirect_request(self, request, fp, code, message, headers, new_url):
        self.redirect_count += 1
        if self.redirect_count > MAX_REDIRECTS:
            raise PublicDownloadError('redirect_limit', 'Link có quá nhiều chuyển hướng.')
        absolute = urllib.parse.urljoin(request.full_url, new_url)
        validate_douyin_redirect(absolute)
        return super().redirect_request(request, fp, code, message, headers, absolute)


def resolve_douyin_video_url(url: str, proxy_url: str) -> str:
    """Resolve share links through the checked proxy and require a video URL."""
    url = validate_douyin_url(url)
    parsed = urlsplit(url)
    canonical = re.fullmatch(r'/video/(\d+)/?', parsed.path)
    if parsed.hostname == 'www.douyin.com' and canonical:
        return f'https://www.douyin.com/video/{canonical.group(1)}'
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}),
        _DouyinRedirectHandler(),
    )
    request = urllib.request.Request(url, headers={'User-Agent': DOUYIN_USER_AGENT, 'Accept': 'text/html,*/*;q=0.8'})
    try:
        with opener.open(request, timeout=CONNECT_TIMEOUT_SECONDS) as response:
            final_url = validate_douyin_redirect(response.geturl())
            final = urlsplit(final_url)
    except PublicDownloadError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PublicDownloadError('douyin_link_unavailable', 'Không thể mở link chia sẻ Douyin an toàn hoặc link đã hết hạn.') from exc
    match = re.fullmatch(r'/video/(\d+)/?', final.path)
    if not match:
        raise PublicDownloadError('douyin_video_required', 'Link phải chuyển tới một video Douyin công khai đơn lẻ.')
    return f'https://www.douyin.com/video/{match.group(1)}'


def download_douyin(url: str, output_dir: Path, max_file_bytes: int, progress_callback=None) -> Path:
    """Download one public Douyin video through the DNS-pinning proxy."""
    url = validate_douyin_url(url)
    if max_file_bytes < 64 * 1024:
        raise PublicDownloadError('public_storage_full', 'Máy chủ chưa đủ dung lượng an toàn cho video tải từ link.')
    output_dir = Path(output_dir).resolve(strict=True)
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise PublicDownloadError('download_storage_failed', 'Không tạo được thư mục tải video an toàn.')

    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    def on_progress(status):
        if time.monotonic() - download_started > MAX_DOWNLOAD_SECONDS:
            raise DownloadError('Douyin download exceeded its wall-clock time limit')
        if progress_callback and status.get('status') == 'downloading':
            downloaded = int(status.get('downloaded_bytes') or 0)
            total = int(status.get('total_bytes') or status.get('total_bytes_estimate') or 0)
            progress_callback(downloaded, total)
        if int(status.get('downloaded_bytes') or 0) > max_file_bytes:
            raise DownloadError('Download exceeded the configured file limit')

    template = str(output_dir / 'source.%(ext)s')
    safe_proxy = SafeDouyinProxy(max_file_bytes, max_runtime_seconds=MAX_METADATA_SECONDS)
    with safe_proxy as proxy_url:
        canonical_url = resolve_douyin_video_url(url, proxy_url)
        options = {
            'proxy': proxy_url,
            'ignoreconfig': True,
            'noplaylist': True,
            'cookiefile': None,
            'usenetrc': False,
            'cachedir': False,
            'quiet': True,
            'no_warnings': True,
            'logger': _QuietLogger(),
            'outtmpl': template,
            'format': 'bestvideo*+bestaudio/best',
            'merge_output_format': 'mp4',
            'max_filesize': int(max_file_bytes),
            'socket_timeout': CONNECT_TIMEOUT_SECONDS,
            'retries': 2,
            'fragment_retries': 2,
            'extractor_retries': 1,
            'file_access_retries': 1,
            'progress_hooks': [on_progress],
        }
        try:
            with YoutubeDL(options) as ydl:
                metadata_started = time.monotonic()
                info = ydl.extract_info(canonical_url, download=False, ie_key='Douyin')
                if time.monotonic() - metadata_started > MAX_METADATA_SECONDS:
                    raise PublicDownloadError('douyin_metadata_timeout', 'Không lấy được metadata Douyin trong 5 phút.')
                if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video'):
                    raise PublicDownloadError('douyin_video_required', 'Link phải trỏ tới một video Douyin đơn lẻ.')
                duration = info.get('duration')
                try:
                    duration = float(duration)
                except (TypeError, ValueError):
                    duration = math.nan
                if not math.isfinite(duration) or duration < 30 or duration > 7200:
                    raise PublicDownloadError('video_duration_limit', 'Video từ link phải dài từ 30 giây đến 2 giờ theo metadata Douyin.')
                hinted_size = info.get('filesize') or info.get('filesize_approx')
                try:
                    if hinted_size is not None and int(hinted_size) > max_file_bytes:
                        raise PublicDownloadError('download_size_limit', 'Video vượt dung lượng tải tối đa hiện tại của máy.')
                except (TypeError, ValueError):
                    pass
                safe_proxy.set_runtime_deadline(MAX_DOWNLOAD_SECONDS)
                download_started = time.monotonic()
                info = ydl.process_ie_result(info, download=True)
        except PublicDownloadError:
            raise
        except DownloadError as exc:
            detail = str(exc).lower()
            if any(term in detail for term in ('login', 'log in', 'cookie', 'private video', 'sign in', 'authentication')):
                raise PublicDownloadError('douyin_cookies_required', 'Link này cần cookies hoặc quyền đăng nhập. Dịch vụ công khai không dùng cookies của chủ máy.') from exc
            if 'timed out' in detail or 'timeout' in detail:
                raise PublicDownloadError('douyin_metadata_timeout', 'Kết nối tới Douyin hoặc CDN vượt thời gian cho phép.') from exc
            if 'exceeded its wall-clock time limit' in str(exc).lower():
                raise PublicDownloadError('douyin_download_timeout', 'Tải video vượt quá giới hạn thời gian 2 giờ.') from exc
            if 'exceeded the configured file limit' in str(exc).lower():
                raise PublicDownloadError('download_size_limit', 'Video vượt dung lượng tải tối đa hiện tại của máy.') from exc
            raise PublicDownloadError('douyin_download_failed', 'Không tải được video Douyin công khai. Link có thể hết hạn, bị giới hạn vùng hoặc cần đăng nhập.') from exc
        except Exception as exc:
            raise PublicDownloadError('douyin_download_failed', 'Không tải được video Douyin công khai. Hãy thử lại với link video công khai khác.') from exc
    if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video'):
        raise PublicDownloadError('douyin_video_required', 'Link phải trỏ tới một video Douyin đơn lẻ.')
    candidates = [
        path for path in output_dir.glob('source.*')
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in SAFE_MEDIA_EXTENSIONS
    ]
    if not candidates:
        raise PublicDownloadError('douyin_download_failed', 'Không tìm thấy video sau khi tải link Douyin.')
    video = max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve(strict=True)
    if video.parent != output_dir or video.stat().st_size < 64 * 1024:
        raise PublicDownloadError('invalid_video', 'Video tải về không hợp lệ hoặc quá nhỏ.')
    if video.stat().st_size > max_file_bytes:
        video.unlink(missing_ok=True)
        raise PublicDownloadError('download_size_limit', 'Video tải về vượt dung lượng tối đa hiện tại của máy.')
    return video
