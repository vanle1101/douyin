import http.cookiejar
import http.client
import concurrent.futures
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

BASE = pathlib.Path(__file__).resolve().parents[1]
VERIFY_ROOT = BASE / 'public-demo-verification'
VERIFY_ROOT.mkdir(exist_ok=True)
_TEMP = tempfile.TemporaryDirectory(prefix='security-tests-', dir=str(VERIFY_ROOT))
os.environ['PUBLIC_DEMO_DATA_DIR'] = _TEMP.name
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['PYTHONIOENCODING'] = 'utf-8'

import public_demo as pd


class PublicDemoSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pd.init_db()
        cls.server = pd.make_server('127.0.0.1', 0)
        cls.port = cls.server.server_address[1]
        cls.base_url = f'http://127.0.0.1:{cls.port}'
        cls.origin = cls.base_url
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()
        cls.owner_a = cls.new_opener()
        cls.owner_b = cls.new_opener()
        for opener in (cls.owner_a, cls.owner_b):
            status, _, _ = cls.request(opener, '/api/public/session')
            if status != 200:
                raise RuntimeError(f'could not create test session: {status}')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        _TEMP.cleanup()

    @classmethod
    def new_opener(cls):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener._test_cookiejar = jar
        return opener

    @classmethod
    def request(cls, opener, path, method='GET', body=None, headers=None):
        headers = dict(headers or {})
        if method in ('POST', 'PUT', 'DELETE'):
            headers.setdefault('Origin', cls.origin)
        data = body
        if isinstance(body, dict):
            data = json.dumps(body).encode('utf-8')
            headers.setdefault('Content-Type', 'application/json')
        req = urllib.request.Request(cls.base_url + path, data=data, headers=headers, method=method)
        try:
            response = opener.open(req, timeout=15)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    @classmethod
    def cookie_header(cls, opener):
        return '; '.join(f'{cookie.name}={cookie.value}' for cookie in opener._test_cookiejar)

    @classmethod
    def session_id(cls, opener):
        for cookie in opener._test_cookiejar:
            if cookie.name == pd.SESSION_COOKIE:
                return pd.hash_value(cookie.value)
        raise AssertionError('session cookie was not set')

    @classmethod
    def make_video(cls, duration, name):
        pd.ensure_layout()
        target = pd.UPLOADS_DIR / f'{uuid.uuid4().hex}-{name}.mp4'
        command = [
            pd.PUBLIC_CFG['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', f'color=c=black:s=160x90:r=1:d={duration}',
            '-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=44100:duration={duration}',
            '-t', str(duration), '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '32k', str(target),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=90)
        return target

    @classmethod
    def seed_job(cls, session_id, status, output=False):
        job_id = uuid.uuid4().hex
        folder = pd.JOBS_DIR / job_id
        folder.mkdir(parents=True, exist_ok=True)
        if output:
            (folder / 'output.mp4').write_bytes(bytes(range(64)) * 8)
            (folder / 'subtitles.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nXin chào\n', encoding='utf-8')
        now = time.time()
        job = {
            'id': job_id, 'title': 'security-test', 'status': status, 'current_stage': None,
            'stages': {stage: 'waiting' for stage in pd.STAGES}, 'stage_times': {},
            'duration': 30, 'progress': 100 if status == 'completed' else 1,
            'sentences': 1, 'chunks': 1, 'created_at': now, 'updated_at': now,
            'output_path': str(folder / 'output.mp4') if output else '',
            'source_path': str(folder / 'source.mp4'), 'error': None,
        }
        with pd.db_connect() as db:
            db.execute('INSERT INTO jobs(id,session_id,data,created,updated) VALUES(?,?,?,?,?)', (job_id, session_id, json.dumps(job), now, now))
        return job_id

    def test_pipeline_readiness_fails_closed_on_low_resources(self):
        good_tools = {'ffmpeg': True, 'ffprobe': True, 'faster_whisper': True}
        fake = {
            'tools': good_tools, 'recognition_model_ready': True, 'ollama_ready': True,
            'translation_model_ready': True, 'voice_ready': True,
            'memory': {'free_mib': 500}, 'gpu': {'available': True, 'free_mib': 100},
        }
        with mock.patch.object(pd, 'preflight', return_value=fake), mock.patch.object(pd.shutil, 'disk_usage', return_value=type('D', (), {'free': 100 * 1024**3})()):
            pd._HEALTH_CACHE = None
            health = pd.pipeline_health(force=True)
        self.assertFalse(health['ready'])
        self.assertEqual(health['resources']['ram'], 'below_safe_threshold')
        self.assertEqual(health['resources']['vram'], 'below_safe_threshold')
        pd._HEALTH_CACHE = None

    def test_wrong_origin_and_options_do_not_get_cors(self):
        status, body, _ = self.request(self.owner_a, '/api/public/uploads/start', 'POST', {'name': 'x.mp4', 'size': 100000}, {'Origin': 'https://attacker.invalid'})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)['code'], 'origin_rejected')
        status, _, headers = self.request(self.owner_a, '/api/public/jobs', 'OPTIONS', headers={'Origin': 'https://attacker.invalid'})
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get('Access-Control-Allow-Origin'))

    def test_cookie_is_httponly_and_secure_for_verified_tunnel(self):
        origin = 'https://security-check.trycloudflare.com'
        pd.write_origin(origin)
        opener = self.new_opener()
        headers = {
            'Host': 'security-check.trycloudflare.com', 'CF-Connecting-IP': '8.8.8.8',
            'CF-Ray': '1a2b3c4d5e6f7890-abc', 'CF-Visitor': '{"scheme":"https"}',
        }
        status, _, response_headers = self.request(opener, '/api/public/session', headers=headers)
        self.assertEqual(status, 200)
        cookie = response_headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('SameSite=Strict', cookie)
        pd.ORIGIN_PATH.unlink(missing_ok=True)

    def test_unknown_host_and_owner_routes_are_not_served(self):
        status, body, _ = self.request(self.owner_a, '/', headers={'Host': 'attacker.invalid'})
        self.assertEqual(status, 421)
        for route in ('/api/state', '/api/config', '/api/checklist', '/data/studio.sqlite3'):
            status, _, _ = self.request(self.owner_a, route)
            self.assertEqual(status, 404, route)

    def test_session_isolation_private_assets_and_range(self):
        self.assertNotEqual(self.session_id(self.owner_a), self.session_id(self.owner_b))
        job_id = self.seed_job(self.session_id(self.owner_a), 'completed', output=True)
        status, body, _ = self.request(self.owner_a, '/api/public/state')
        self.assertEqual(status, 200)
        self.assertIn(job_id, [job['id'] for job in json.loads(body)['jobs']])
        status, body, _ = self.request(self.owner_b, '/api/public/state')
        self.assertEqual(status, 200)
        self.assertNotIn(job_id, [job['id'] for job in json.loads(body)['jobs']])
        status, _, _ = self.request(self.owner_b, f'/api/public/jobs/{job_id}')
        self.assertEqual(status, 404)
        status, _, _ = self.request(self.owner_b, f'/api/public/jobs/{job_id}/output')
        self.assertEqual(status, 404)
        status, _, _ = self.request(self.owner_a, f'/api/public/jobs/{job_id}/source')
        self.assertEqual(status, 404)
        req = urllib.request.Request(self.base_url + f'/api/public/jobs/{job_id}/output', headers={'Range': 'bytes=2-5'})
        response = self.owner_a.open(req, timeout=10)
        self.assertEqual(response.status, 206)
        self.assertEqual(response.headers.get('Content-Range'), 'bytes 2-5/512')
        self.assertEqual(len(response.read()), 4)

    def test_quick_tunnel_denies_mp4_but_serves_owner_subtitles(self):
        job_id = self.seed_job(self.session_id(self.owner_a), 'completed', output=True)
        origin = 'https://security-check.trycloudflare.com'
        pd.write_origin(origin)
        forwarded = {
            'Host': 'security-check.trycloudflare.com',
            'CF-Connecting-IP': '8.8.8.8',
            'CF-Ray': '1a2b3c4d5e6f7890-abc',
            'CF-Visitor': '{"scheme":"https"}',
        }
        try:
            status, body, _ = self.request(
                self.owner_a, f'/api/public/jobs/{job_id}/output',
                headers={**forwarded, 'Range': 'bytes=0-15'},
            )
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)['code'], 'video_delivery_unavailable')
            status, body, _ = self.request(
                self.owner_a, f'/api/public/jobs/{job_id}/subtitles?download=1', headers=forwarded,
            )
            self.assertEqual(status, 200)
            self.assertIn(b'Xin ch\xc3\xa0o', body)
        finally:
            pd.ORIGIN_PATH.unlink(missing_ok=True)

    def test_only_owner_can_cancel_job(self):
        job_id = self.seed_job(self.session_id(self.owner_a), 'queued')
        status, body, _ = self.request(self.owner_b, f'/api/public/jobs/{job_id}/cancel', 'POST')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['code'], 'job_not_found')
        status, body, _ = self.request(self.owner_a, f'/api/public/jobs/{job_id}/cancel', 'POST')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'cancelled')
        status, body, _ = self.request(self.owner_b, f'/api/public/jobs/{job_id}/delete', 'POST')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['code'], 'job_not_found')
        status, body, _ = self.request(self.owner_a, f'/api/public/jobs/{job_id}/delete', 'POST')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['deleted'])

    def test_request_threads_are_bounded_under_burst(self):
        server = pd.BoundedThreadingHTTPServer(('127.0.0.1', 0), pd.Handler, max_threads=2)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_address[1]}/api/public/health'

        def fetch():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code

        try:
            with mock.patch.object(pd, 'pipeline_health', side_effect=lambda: (time.sleep(0.35), {'ready': True})[1]):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    statuses = list(pool.map(lambda _: fetch(), range(8)))
            self.assertIn(200, statuses)
            self.assertIn(503, statuses)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @mock.patch.object(pd, 'pipeline_health', return_value={'ready': True})
    def test_upload_size_chunk_and_link_source_rejection(self, _ready):
        status, body, _ = self.request(self.owner_a, '/api/public/uploads/start', 'POST', {'name': 'too-large.mp4', 'size': pd.MAX_UPLOAD_HARD_CAP_BYTES + 1})
        self.assertEqual(status, 413)
        for candidate in (
            'https://127.0.0.1/latest/meta-data/',
            'https://v.douyin.com.attacker.invalid/abc',
            'https://v.douyin.com@127.0.0.1/abc',
        ):
            status, body, _ = self.request(self.owner_a, '/api/public/jobs', 'POST', {'url': candidate})
            self.assertEqual(status, 400, candidate)
            self.assertEqual(json.loads(body)['code'], 'unsupported_source', candidate)
        size = pd.MAX_CHUNK_BYTES + 5
        status, body, _ = self.request(self.owner_a, '/api/public/uploads/start', 'POST', {'name': 'chunk-test.mp4', 'size': size})
        self.assertEqual(status, 201)
        upload_id = json.loads(body)['upload']
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.putrequest('POST', f'/api/public/uploads/{upload_id}')
        conn.putheader('Host', f'127.0.0.1:{self.port}')
        conn.putheader('Origin', self.origin)
        conn.putheader('Cookie', self.cookie_header(self.owner_a))
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('X-Offset', '0')
        conn.putheader('Content-Length', str(pd.MAX_CHUNK_BYTES + 1))
        conn.endheaders(b'x')
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        conn.close()

    @mock.patch.object(pd, 'pipeline_health', return_value={'ready': True})
    def test_public_api_rejects_douyin_link_sources(self, _ready):
        owner = self.new_opener()
        self.request(owner, '/api/public/session')
        for source in (
            {'url': 'https://www.douyin.com/video/6961737553342991651'},
            {'link': 'https://v.douyin.com/abc123/'},
        ):
            status, body, _ = self.request(owner, '/api/public/jobs', 'POST', source)
            self.assertEqual(status, 400, body)
            self.assertEqual(json.loads(body)['code'], 'unsupported_source')
        session_id = self.session_id(owner)
        with pd.db_connect() as db:
            created = db.execute('SELECT COUNT(*) FROM jobs WHERE session_id=?', (session_id,)).fetchone()[0]
        self.assertEqual(created, 0)
        self.assertFalse(hasattr(pd, 'LINK_INPUT_ENABLED'))

    @mock.patch.object(pd, 'pipeline_health', return_value={'ready': True})
    def test_upload_resume_returns_committed_offset(self, _ready):
        opener = self.new_opener()
        self.request(opener, '/api/public/session')
        size = 64 * 1024
        fingerprint = 'a' * 64
        body = {'name': 'resume.mp4', 'size': size, 'fingerprint': fingerprint}
        status, response, _ = self.request(opener, '/api/public/uploads/start', 'POST', body)
        self.assertEqual(status, 201)
        upload_id = json.loads(response)['upload']
        chunk = b'x' * 1024
        status, response, _ = self.request(opener, f'/api/public/uploads/{upload_id}', 'POST', chunk, {
            'Content-Type': 'application/octet-stream', 'X-Offset': '0',
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)['received'], len(chunk))
        status, response, _ = self.request(opener, f'/api/public/uploads/{upload_id}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)['received'], len(chunk))
        status, response, _ = self.request(opener, '/api/public/uploads/start', 'POST', body)
        self.assertEqual(status, 200)
        resumed = json.loads(response)
        self.assertTrue(resumed['resumed'])
        self.assertEqual(resumed['upload'], upload_id)
        self.assertEqual(resumed['received'], len(chunk))

    @mock.patch.object(pd, 'pipeline_health', return_value={'ready': True})
    def test_video_duration_and_queue_caps(self, _ready):
        long_valid = self.make_video(301, 'over-five-min-valid')
        self.assertGreater(pd.probe_public_video(long_valid), 300)
        too_long = pd.UPLOADS_DIR / f'{uuid.uuid4().hex}-over-two-hour.mp4'
        too_long.write_bytes(bytes(range(256)) * 256)
        with self.assertRaises(pd.PublicError) as caught:
            with mock.patch.object(pd.subprocess, 'run', return_value=mock.Mock(
                returncode=0,
                stdout='{"format":{"duration":"7201"},"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
            )):
                pd.probe_public_video(too_long)
        self.assertEqual(caught.exception.code, 'video_duration_limit')
        good = self.make_video(30, 'queue-full')
        owner = self.session_id(self.owner_b)
        upload_id = uuid.uuid4().hex
        stored = uuid.uuid4().hex + '.mp4'
        target = pd.UPLOADS_DIR / stored
        good.replace(target)
        now = time.time()
        with pd.db_connect() as db:
            db.execute('INSERT INTO uploads(id,session_id,filename,expected_size,received,stored_name,created,updated,consumed) VALUES(?,?,?,?,?,?,?,?,0)', (upload_id, owner, 'queue-full.mp4', target.stat().st_size, target.stat().st_size, stored, now, now))
        self.seed_job(owner, 'running')
        for _ in range(pd.MAX_WAITING_JOBS):
            self.seed_job(owner, 'queued')
        status, body, _ = self.request(self.owner_b, '/api/public/jobs', 'POST', {'upload': upload_id})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)['code'], 'public_queue_full')

    def test_two_hour_queue_eta_and_full_queue_behavior(self):
        with tempfile.TemporaryDirectory(prefix='queue-eta-', dir=str(VERIFY_ROOT)) as temporary, \
             mock.patch.object(pd, 'DB_PATH', pathlib.Path(temporary) / 'queue.sqlite3'):
            pd.init_db()
            now = time.time()
            queued = []
            db_conn = pd.db_connect()
            with db_conn as db:
                for index in range(11):
                    job_id = f'{index + 1:032x}'
                    status = 'running' if index == 0 else 'queued'
                    created = now + index
                    job = {
                        'id': job_id, 'session_id': f'session-{index}', 'title': 'two-hour-queue-check',
                        'status': status, 'created_at': created, 'updated_at': created,
                        'duration': 7200, 'progress': 0, 'stages': {stage: 'waiting' for stage in pd.STAGES},
                        'processing_started_at': now if status == 'running' else None,
                    }
                    db.execute('INSERT INTO jobs(id,session_id,data,created,updated) VALUES(?,?,?,?,?)',
                               (job_id, job['session_id'], json.dumps(job), created, created))
                    if status == 'queued':
                        queued.append(job)
            db_conn.close()

            with mock.patch.object(pd, 'processing_time_estimate', return_value=(10_080, 1)):
                observed = pd.public_job(queued[-1])
            self.assertEqual(observed['queue_position'], 10)
            self.assertEqual(observed['estimated_wait_seconds'], 28 * 3600)
            self.assertEqual(observed['estimated_wait_basis'], 'history')
            self.assertEqual(observed['estimate_sample_count'], 1)
            self.assertEqual(observed['estimated_processing_seconds'], 10_080)

            with mock.patch.object(pd, 'processing_time_estimate', return_value=(None, 0)):
                conservative = pd.public_job(queued[-1])
            self.assertEqual(conservative['estimated_wait_seconds'], 60 * 3600)
            self.assertEqual(conservative['estimated_wait_basis'], 'runtime_limit')
            self.assertEqual(conservative['estimate_sample_count'], 0)

    def test_storage_never_points_into_owner_data(self):
        self.assertNotEqual(pd.DB_PATH.resolve(), (BASE / 'data' / 'studio.sqlite3').resolve())
        self.assertFalse(pd.PUBLIC_ROOT.is_relative_to((BASE / 'data').resolve()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
