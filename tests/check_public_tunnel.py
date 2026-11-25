"""Check the current Quick Tunnel and one prior demo job without exposing cookies."""
import argparse
import http.cookiejar
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--base-url', default=None)
args = parser.parse_args()
if args.base_url:
    base = args.base_url.rstrip('/')
else:
    url_file = BASE / 'public-demo-url.txt'
    if not url_file.is_file():
        raise SystemExit('No public-demo-url.txt; start a verified Quick Tunnel first.')
    base = url_file.read_text(encoding='utf-8-sig').strip().rstrip('/')
parsed_base = urllib.parse.urlsplit(base)
if parsed_base.scheme != 'https' or not parsed_base.hostname or not parsed_base.hostname.endswith('.trycloudflare.com'):
    raise SystemExit('Expected the active HTTPS Quick Tunnel URL.')
origin = f'{parsed_base.scheme}://{parsed_base.netloc}'
db_path = BASE / 'public-demo-data' / 'public-demo.sqlite3'
with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True) as db:
    row = db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 1').fetchone()
HIDDEN_JOB = row[0] if row else None

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request(path, method='GET', body=None, headers=None):
    headers = dict(headers or {})
    if method == 'POST':
        headers.setdefault('Origin', origin)
    data = json.dumps(body).encode('utf-8') if isinstance(body, dict) else body
    if isinstance(body, dict):
        headers.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


status, headers, page = request('/')
assert status == 200 and b'Douyin Studio' in page and b'Gi&#7899;i h&#7841;n demo' not in page
cookie = headers.get('Set-Cookie', '')
status, session_headers, _ = request('/api/public/session')
cookie = cookie or session_headers.get('Set-Cookie', '')
assert status == 200 and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=Strict' in cookie
status, _, body = request('/api/public/state')
assert status == 200
state = json.loads(body)
if HIDDEN_JOB:
    assert not any(job['id'] == HIDDEN_JOB for job in state.get('jobs', [])), 'new session saw an existing job'
    status, _, _ = request(f'/api/public/jobs/{HIDDEN_JOB}/output', headers={'Range': 'bytes=0-15'})
    assert status == 404, f'new session accessed another session output: {status}'
for path in ('/api/state', '/api/config', '/api/checklist', '/api/models', '/api/files'):
    status, _, _ = request(path)
    assert status == 404, f'private owner route unexpectedly served: {path} -> {status}'
for candidate in ('https://127.0.0.1/latest/meta-data/', 'https://v.douyin.com.attacker.invalid/x', 'https://v.douyin.com@127.0.0.1/x'):
    status, _, body = request('/api/public/jobs', 'POST', {'url': candidate})
    assert status == 400 and json.loads(body)['code'] == 'links_disabled', f'unsafe URL not rejected: {candidate}'
status, headers, _ = request('/api/public/jobs', 'OPTIONS', headers={'Origin': 'https://attacker.invalid'})
assert status == 403 and headers.get('Access-Control-Allow-Origin') is None
status, _, body = request('/api/public/health')
health = json.loads(body)
assert status == 200
print(json.dumps({
    'https_page': 'PASS', 'secure_session_cookie': 'PASS', 'session_isolation': 'PASS' if HIDDEN_JOB else 'NO_PRIOR_JOB_TO_CHECK',
    'private_owner_routes': 'PASS', 'cross_origin': 'DENIED', 'douyin_links': 'LOCKED_UNTIL_SSRF_BOUNDARY',
    'health_ready': health.get('ready'), 'resource_status': health.get('resources'),
}, ensure_ascii=False))
