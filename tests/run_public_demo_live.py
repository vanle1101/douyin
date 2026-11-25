"""Live API check: upload one permitted short video, await the real pipeline, fetch output and Range."""
import argparse
import http.cookiejar
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8766')
    parser.add_argument('--video', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    origin = f'{urllib.parse.urlsplit(base).scheme}://{urllib.parse.urlsplit(base).netloc}'
    video = pathlib.Path(args.video).resolve(strict=True)
    output_dir = pathlib.Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(path, method='GET', payload=None, headers=None, timeout=45):
        headers = dict(headers or {})
        if method == 'POST':
            headers.setdefault('Origin', origin)
        body = payload
        if isinstance(payload, dict):
            body = json.dumps(payload).encode('utf-8')
            headers.setdefault('Content-Type', 'application/json')
        req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    status, _, page = request('/')
    if status != 200 or b'Douyin Studio' not in page:
        raise RuntimeError(f'UI page check failed: HTTP {status}')
    status, _, _ = request('/api/public/session')
    if status != 200 or not any(cookie.name == 'pd_session' for cookie in jar):
        raise RuntimeError(f'Anonymous session check failed: HTTP {status}')
    status, _, body = request('/api/public/health')
    health = json.loads(body)
    if status != 200 or not health.get('ready'):
        raise RuntimeError(f'Public pipeline health is not ready: {body.decode(errors="replace")[:500]}')
    size = video.stat().st_size
    status, _, body = request('/api/public/uploads/start', 'POST', {'name': video.name, 'size': size})
    if status not in (200, 201):
        raise RuntimeError(f'Upload start failed: HTTP {status}: {body.decode(errors="replace")[:300]}')
    upload = json.loads(body)['upload']
    chunk_size = json.loads(body)['chunk_bytes']
    sent = 0
    with video.open('rb') as stream:
        while chunk := stream.read(chunk_size):
            headers = {'Content-Type': 'application/octet-stream', 'X-Offset': str(sent), 'Origin': origin}
            status, _, body = request(f'/api/public/uploads/{upload}', 'POST', chunk, headers, timeout=90)
            if status != 200:
                raise RuntimeError(f'Chunk upload failed at {sent}: HTTP {status}: {body.decode(errors="replace")[:300]}')
            sent += len(chunk)
            print(f'UPLOAD {sent}/{size}', flush=True)
    status, _, body = request('/api/public/jobs', 'POST', {'upload': upload})
    if status != 201:
        raise RuntimeError(f'Job creation failed: HTTP {status}: {body.decode(errors="replace")[:300]}')
    job = json.loads(body)
    job_id = job['id']
    print(f'JOB {job_id} CREATED; duration={job.get("duration")}', flush=True)
    deadline = time.monotonic() + int(health['limits']['max_runtime_seconds']) + 90
    last = None
    while time.monotonic() < deadline:
        status, _, body = request(f'/api/public/jobs/{job_id}')
        if status != 200:
            raise RuntimeError(f'Job polling failed: HTTP {status}')
        job = json.loads(body)
        mark = (job.get('status'), job.get('current_stage'), job.get('progress'))
        if mark != last:
            print(f'JOB {mark[0]} · {job.get("stage_label") or ""} · {mark[2]}%', flush=True)
            last = mark
        if job.get('status') == 'completed':
            break
        if job.get('status') == 'blocked':
            _, _, log_body = request(f'/api/public/jobs/{job_id}/log')
            print(f'JOB BLOCKED code={job.get("error")}; safe_log={log_body.decode(errors="replace")[:1200]}', flush=True)
            return 2
        time.sleep(3)
    else:
        raise TimeoutError('Job did not finish within the configured processing time limit.')

    status, headers, body = request(f'/api/public/jobs/{job_id}/output', headers={'Range': 'bytes=0-1023'})
    if status != 206 or headers.get('Content-Range') is None or len(body) != 1024:
        raise RuntimeError(f'Output Range check failed: HTTP {status}, bytes={len(body)}')
    status, _, mp4 = request(f'/api/public/jobs/{job_id}/output?download=1', timeout=90)
    if status != 200 or len(mp4) < 1024 or b'ftyp' not in mp4[:32]:
        raise RuntimeError(f'MP4 download failed: HTTP {status}, bytes={len(mp4)}')
    mp4_path = output_dir / f'{job_id}-output.mp4'
    mp4_path.write_bytes(mp4)
    status, _, srt = request(f'/api/public/jobs/{job_id}/subtitles?download=1')
    if status != 200 or not srt.strip():
        raise RuntimeError(f'SRT download failed: HTTP {status}')
    srt_path = output_dir / f'{job_id}-subtitles.srt'
    srt_path.write_bytes(srt)
    status, _, vtt = request(f'/api/public/jobs/{job_id}/vtt')
    if status != 200 or b'WEBVTT' not in vtt:
        raise RuntimeError(f'VTT preview check failed: HTTP {status}')
    status, _, log_body = request(f'/api/public/jobs/{job_id}/log')
    if status != 200 or 'Job hoàn tất.' not in log_body.decode('utf-8'):
        raise RuntimeError('Safe job log did not show completion.')
    print(f'E2E PASS job={job_id} seconds={job.get("duration")} output_bytes={len(mp4)} srt_bytes={len(srt)}', flush=True)
    print(f'OUTPUT {mp4_path}', flush=True)
    print(f'SUBTITLES {srt_path}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'E2E FAILED: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        raise
