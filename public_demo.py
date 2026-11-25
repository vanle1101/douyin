"""Isolated public video service; the owner engine remains on 127.0.0.1:8765."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BASE = pathlib.Path(__file__).resolve().parent
PACKAGE_OVERLAY = BASE / '.public-demo-env' / 'Lib' / 'site-packages'
if PACKAGE_OVERLAY.is_dir() and str(PACKAGE_OVERLAY) not in sys.path:
    sys.path.insert(0, str(PACKAGE_OVERLAY))
os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

from engine import LABELS, STAGES, preflight, process_stage, read_json, system_memory, write_json  # noqa: E402

PUBLIC_ROOT = pathlib.Path(os.environ.get('PUBLIC_DEMO_DATA_DIR', str(BASE / 'public-demo-data'))).resolve()
OWNER_DATA = (BASE / 'data').resolve()
if PUBLIC_ROOT == OWNER_DATA or PUBLIC_ROOT.is_relative_to(OWNER_DATA):
    raise RuntimeError('PUBLIC_DEMO storage must be separate from LOCAL_OWNER data/')
if not PUBLIC_ROOT.is_relative_to(BASE.resolve()):
    raise RuntimeError('PUBLIC_DEMO storage must remain inside the project folder')
DB_PATH = PUBLIC_ROOT / 'public-demo.sqlite3'
UPLOADS_DIR = PUBLIC_ROOT / 'uploads'
JOBS_DIR = PUBLIC_ROOT / 'jobs'
ORIGIN_PATH = PUBLIC_ROOT / 'public-origin.json'
SERVER_PID_PATH = PUBLIC_ROOT / 'server-process.json'
SERVER_RUNTIME_PATH = PUBLIC_ROOT / 'server-runtime.json'
TUNNEL_PID_PATH = PUBLIC_ROOT / 'tunnel-process.json'
PUBLIC_DIST = BASE / 'public_dist'

MAX_VIDEO_SECONDS = 2 * 60 * 60
MIN_VIDEO_SECONDS = 30
MAX_UPLOAD_HARD_CAP_BYTES = 8 * 1024 * 1024 * 1024
MAX_CHUNK_BYTES = 16 * 1024 * 1024
MAX_JOB_SECONDS = 6 * 60 * 60
MAX_JOB_STORAGE_BYTES = 28 * 1024 * 1024 * 1024
MAX_PUBLIC_STORAGE_BYTES = 32 * 1024 * 1024 * 1024
PUBLIC_STORAGE_SAFETY_BYTES = 64 * 1024 * 1024
MAX_ACTIVE_JOBS = 1
MAX_WAITING_JOBS = 10
MAX_SESSION_JOBS_HOUR = 2
MAX_SESSION_JOBS_DAY = 8
MAX_IP_JOBS_HOUR = 2
MAX_IP_JOBS_DAY = 8
MAX_GLOBAL_JOBS_HOUR = 4
MAX_GLOBAL_JOBS_DAY = 16
MP4_DELIVERY_VIA_TUNNEL = False
MIN_FREE_RAM_MIB = 8192
MIN_FREE_VRAM_MIB = 1800
MIN_FREE_DISK_MIB = 10 * 1024
UPLOAD_TTL_SECONDS = 24 * 60 * 60
SESSION_TTL_SECONDS = 24 * 60 * 60
SESSION_COOKIE = 'pd_session'
LOCAL_ORIGINS_HOSTS = {'127.0.0.1', 'localhost'}
SAFE_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm'}

FFMPEG_CANDIDATES = [
    pathlib.Path(r'E:\Train giọng\tools\ffmpeg\bin\ffmpeg.exe'),
    pathlib.Path(r'E:\Train giọng\tools\applio\ffmpeg.exe'),
]
FFPROBE_CANDIDATES = [
    pathlib.Path(r'E:\Train giọng\tools\ffmpeg\bin\ffprobe.exe'),
    pathlib.Path(r'E:\Train giọng\tools\applio\ffprobe.exe'),
]


class PublicError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = 'invalid_request', retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def ensure_layout() -> None:
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(exist_ok=True)
    JOBS_DIR.mkdir(exist_ok=True)


def db_connect() -> sqlite3.Connection:
    ensure_layout()
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=10000')
    return db


def init_db() -> None:
    ensure_layout()
    with db_connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(
                id_hash TEXT PRIMARY KEY, ip_hash TEXT NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS uploads(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, filename TEXT NOT NULL,
                expected_size INTEGER NOT NULL, received INTEGER NOT NULL, stored_name TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs(session_id, created DESC);
            CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(json_extract(data, '$.status'), created);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                message TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_job_id ON events(job_id, id);
        ''')
        upload_columns = {row['name'] for row in db.execute('PRAGMA table_info(uploads)').fetchall()}
        if 'fingerprint' not in upload_columns:
            db.execute("ALTER TABLE uploads ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")


def resolve_hf_snapshot(repo: str) -> pathlib.Path | None:
    cache_root = pathlib.Path(os.environ.get('HF_HUB_CACHE') or (pathlib.Path(os.environ.get('HF_HOME') or (pathlib.Path.home() / '.cache' / 'huggingface')) / 'hub'))
    repo_dir = cache_root / ('models--' + repo.replace('/', '--'))
    try:
        revision = (repo_dir / 'refs' / 'main').read_text(encoding='utf-8').strip()
        snapshot = repo_dir / 'snapshots' / revision
        return snapshot if snapshot.is_dir() else None
    except OSError:
        return None


def first_existing(paths: list[pathlib.Path], fallback: str) -> str:
    for path in paths:
        if path.is_file():
            return str(path)
    return shutil.which(fallback) or fallback


VIE_NEU_ROOT = resolve_hf_snapshot('pnnbao-ump/VieNeu-TTS-v3-Turbo')
MOSS_ROOT = resolve_hf_snapshot('OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX')
PUBLIC_CFG = {
    'profile_mode': 'manual',
    'source_language': 'auto',
    'target_language': 'vi',
    'recognition_model': 'small',
    'cpu_fallback_model': 'base',
    'translation_model': 'qwen2.5:3b-viet',
    'voice_path': '',
    'tts_provider': 'vieneu',
    'tts_command': [],
    'gpu_policy': 'auto',
    'vram_floor_mib': MIN_FREE_VRAM_MIB,
    'min_free_ram_mib': MIN_FREE_RAM_MIB,
    'cpu_threads': 2,
    'parallel_cpu': 1,
    'chunk_seconds': 90,
    'encode_preset': 'ultrafast',
    'crf': 25,
    'ffmpeg': first_existing(FFMPEG_CANDIDATES, 'ffmpeg'),
    'ffprobe': first_existing(FFPROBE_CANDIDATES, 'ffprobe'),
    'yt_dlp': str(BASE / '.public-demo-env' / 'Scripts' / 'yt-dlp.exe'),
    'cookies_path': '',
    'piper': 'piper',
    'ollama_url': 'http://127.0.0.1:11434',
    'vieneu_onnx_dir': str(VIE_NEU_ROOT / 'onnx_update') if VIE_NEU_ROOT else '',
    'vieneu_codec_dir': str(MOSS_ROOT) if MOSS_ROOT else '',
}


def data_size(root: pathlib.Path = PUBLIC_ROOT) -> int:
    total = 0
    try:
        for path in root.rglob('*'):
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def estimated_job_storage(source_bytes: int, duration: float = MAX_VIDEO_SECONDS) -> int:
    source_bytes = max(0, int(source_bytes))
    audio_bytes = max(0, int(math.ceil(max(0.0, float(duration)) * 32000)))
    return min(MAX_JOB_STORAGE_BYTES, source_bytes * 3 + audio_bytes + 512 * 1024 * 1024)


def public_storage_budget_bytes() -> int:
    try:
        free = shutil.disk_usage(PUBLIC_ROOT).free
    except OSError:
        return data_size()
    usable = max(0, free - MIN_FREE_DISK_MIB * 1024 * 1024)
    return min(MAX_PUBLIC_STORAGE_BYTES, data_size() + usable)


def reserved_job_storage(db: sqlite3.Connection) -> int:
    rows = db.execute("SELECT id,data FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested','queued')").fetchall()
    reserved = 0
    for row in rows:
        try:
            job = json.loads(row['data'])
            limit = min(MAX_JOB_STORAGE_BYTES, max(0, int(job.get('storage_limit_bytes') or MAX_JOB_STORAGE_BYTES)))
            used = data_size(JOBS_DIR / row['id'])
            source = pathlib.Path(str(job.get('upload_path') or ''))
            try:
                resolved = source.resolve(strict=True)
                if resolved.parent == UPLOADS_DIR.resolve() and resolved.is_file() and not resolved.is_symlink():
                    used += resolved.stat().st_size
            except (OSError, RuntimeError):
                pass
            reserved += max(0, limit - used)
        except (TypeError, ValueError, OSError):
            reserved += MAX_JOB_STORAGE_BYTES
    return reserved


def pending_upload_storage(db: sqlite3.Connection, now: float | None = None, exclude_id: str = '') -> int:
    now = time.time() if now is None else now
    rows = db.execute(
        'SELECT id,expected_size,received FROM uploads WHERE consumed=0 AND created>? AND id<>?',
        (now - UPLOAD_TTL_SECONDS, exclude_id),
    ).fetchall()
    return sum(max(0, estimated_job_storage(int(row['expected_size'])) - int(row['received'])) for row in rows)


def available_storage_bytes(db: sqlite3.Connection, now: float | None = None, exclude_upload: str = '') -> int:
    now = time.time() if now is None else now
    try:
        free = shutil.disk_usage(PUBLIC_ROOT).free
    except OSError:
        return 0
    reserved = reserved_job_storage(db)
    pending = pending_upload_storage(db, now, exclude_upload)
    disk_budget = max(0, free - MIN_FREE_DISK_MIB * 1024 * 1024 - reserved - pending)
    public_budget = max(0, public_storage_budget_bytes() - data_size() - reserved - pending)
    return max(0, min(disk_budget, public_budget) - PUBLIC_STORAGE_SAFETY_BYTES)


def max_upload_bytes(db: sqlite3.Connection | None = None) -> int:
    if db is None:
        with db_connect() as conn:
            return max_upload_bytes(conn)
    available = available_storage_bytes(db)
    fixed = int(math.ceil(MAX_VIDEO_SECONDS * 32000)) + 512 * 1024 * 1024
    return max(0, min(MAX_UPLOAD_HARD_CAP_BYTES, (available - fixed) // 3))


def job_runtime_limit(duration: float) -> int:
    return min(MAX_JOB_SECONDS, max(30 * 60, 30 * 60 + int(max(0.0, duration) * 3)))


def processing_time_estimate(duration: float) -> tuple[int | None, int]:
    try:
        with db_connect() as db:
            rows = db.execute(
                "SELECT data FROM jobs WHERE json_extract(data,'$.status')='completed' ORDER BY updated DESC LIMIT 20"
            ).fetchall()
        ratios = []
        for row in rows:
            job = json.loads(row['data'])
            video_seconds = float(job.get('duration') or 0)
            runtime_seconds = float(job.get('runtime_seconds') or 0)
            if video_seconds > 0 and runtime_seconds > 0 and math.isfinite(video_seconds) and math.isfinite(runtime_seconds):
                ratios.append(runtime_seconds / video_seconds)
        if not ratios:
            return None, 0
        ratio = sorted(ratios)[len(ratios) // 2]
        estimate = max(60, int(max(0.0, duration) * ratio))
        return min(MAX_JOB_SECONDS, estimate), len(ratios)
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None, 0


def estimate_queue_wait_seconds(target: dict) -> tuple[int | None, int, str]:
    if target.get('status') != 'queued':
        return None, 0, 'not_queued'
    target_key = (float(target.get('created_at') or 0), str(target.get('id') or ''))
    try:
        with db_connect() as db:
            rows = db.execute(
                "SELECT id,data FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested','queued') ORDER BY created,id"
            ).fetchall()
    except sqlite3.Error:
        return None, 0, 'unavailable'
    wait = 0
    sample_counts = []
    now = time.time()
    found = False
    for row in rows:
        job = json.loads(row['data'])
        key = (float(job.get('created_at') or 0), str(row['id']))
        if key == target_key:
            found = True
            break
        duration = float(job.get('duration') or MAX_VIDEO_SECONDS)
        estimate, samples = processing_time_estimate(duration)
        sample_counts.append(samples)
        if estimate is None:
            estimate = job_runtime_limit(duration)
        if job.get('status') in ('running', 'cancel_requested'):
            elapsed = max(0, int(now - float(job.get('processing_started_at') or job.get('created_at') or now)))
            estimate = max(0, estimate - elapsed)
        wait += estimate
    if not found:
        return None, 0, 'unavailable'
    samples = min((count for count in sample_counts if count > 0), default=0)
    basis = 'history' if samples else 'runtime_limit'
    return wait, samples, basis


def active_counts() -> tuple[int, int]:
    try:
        with db_connect() as db:
            rows = db.execute("SELECT json_extract(data,'$.status') AS status, COUNT(*) AS n FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested','queued') GROUP BY status").fetchall()
        counts = {row['status']: row['n'] for row in rows}
        return int(counts.get('running', 0)) + int(counts.get('cancel_requested', 0)), int(counts.get('queued', 0))
    except sqlite3.Error:
        return 0, 0


def quota_resources(health: dict | None = None) -> dict:
    if health is None:
        health = preflight(PUBLIC_CFG)
    memory = health.get('memory') or system_memory() or {}
    ram_free = int(memory.get('free_mib') or 0)
    gpu = health.get('gpu') or {}
    gpu_available = bool(gpu.get('available'))
    vram_free = gpu.get('free_mib')
    gpu_usable = gpu_available and int(vram_free or 0) >= MIN_FREE_VRAM_MIB
    cpu_fallback_ok = ram_free >= 12 * 1024
    try:
        free_disk = shutil.disk_usage(PUBLIC_ROOT).free // (1024 * 1024)
    except OSError:
        free_disk = 0
    return {
        'ram_ok': ram_free >= MIN_FREE_RAM_MIB,
        'vram_ok': gpu_usable or cpu_fallback_ok,
        'disk_ok': free_disk >= MIN_FREE_DISK_MIB,
        'ram_free_mib': ram_free,
        'vram_free_mib': int(vram_free or 0) if gpu_available else None,
        'gpu_available': gpu_available,
        'gpu_usable': gpu_usable,
        'cpu_fallback_ok': cpu_fallback_ok,
        'free_disk_mib': int(free_disk),
    }


_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE: tuple[float, dict] | None = None


def pipeline_health(force: bool = False) -> dict:
    global _HEALTH_CACHE
    now = time.monotonic()
    with _HEALTH_LOCK:
        if not force and _HEALTH_CACHE and now - _HEALTH_CACHE[0] < 8:
            return _HEALTH_CACHE[1]
        result = preflight(PUBLIC_CFG)
        resources = quota_resources(result)
        tools = result.get('tools') or {}
        pipeline = {
            'video_tools': bool(tools.get('ffmpeg') and tools.get('ffprobe')),
            'speech_recognition': bool(tools.get('faster_whisper') and result.get('recognition_model_ready')),
            'translation': bool(result.get('ollama_ready') and result.get('translation_model_ready')),
            'vietnamese_voice': bool(result.get('voice_ready')),
        }
        running, queued = active_counts()
        ready = all(pipeline.values()) and all(resources[k] for k in ('ram_ok', 'vram_ok', 'disk_ok')) and data_size() < public_storage_budget_bytes()
        safe = {
            'ready': ready,
            'pipeline': pipeline,
            'resources': {
                'ram': 'safe' if resources['ram_ok'] else 'below_safe_threshold',
                'vram': 'safe' if resources['gpu_usable'] else ('cpu_fallback' if resources['cpu_fallback_ok'] else 'below_safe_threshold'),
                'disk': 'safe' if resources['disk_ok'] else 'below_safe_threshold',
                'ram_free_mib': resources['ram_free_mib'],
                'min_free_ram_mib': MIN_FREE_RAM_MIB,
                'vram_free_mib': resources['vram_free_mib'],
                'min_free_vram_mib': MIN_FREE_VRAM_MIB,
                'cpu_fallback_ram_mib': 12 * 1024,
                'disk_free_mib': resources['free_disk_mib'],
                'running': running,
                'waiting': queued,
            },
            'limits': {
                'min_video_seconds': MIN_VIDEO_SECONDS,
                'max_video_seconds': MAX_VIDEO_SECONDS,
                'max_upload_bytes': max_upload_bytes(),
                'max_upload_hard_cap_bytes': MAX_UPLOAD_HARD_CAP_BYTES,
                'max_runtime_seconds': MAX_JOB_SECONDS,
                'max_job_storage_bytes': MAX_JOB_STORAGE_BYTES,
                'max_public_storage_bytes': public_storage_budget_bytes(),
                'min_free_disk_mib': MIN_FREE_DISK_MIB,
                'output_retention_policy': 'Retained until the public storage budget or disk reserve prevents new work; no automatic age expiry.',
                'mp4_delivery_via_tunnel': MP4_DELIVERY_VIA_TUNNEL,
                'max_active_jobs': MAX_ACTIVE_JOBS,
                'max_waiting_jobs': MAX_WAITING_JOBS,
                'jobs_per_ip_hour': MAX_IP_JOBS_HOUR,
                'jobs_per_ip_day': MAX_IP_JOBS_DAY,
                'jobs_per_session_hour': MAX_SESSION_JOBS_HOUR,
                'jobs_per_session_day': MAX_SESSION_JOBS_DAY,
                'jobs_global_hour': MAX_GLOBAL_JOBS_HOUR,
                'jobs_global_day': MAX_GLOBAL_JOBS_DAY,
            },
        }
        _HEALTH_CACHE = (now, safe)
        return safe


def origin_from_file() -> str:
    try:
        value = json.loads(ORIGIN_PATH.read_text(encoding='utf-8')).get('origin', '')
    except (OSError, ValueError, AttributeError):
        return ''
    try:
        return validate_public_origin(str(value))
    except ValueError:
        return ''


def validate_public_origin(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    pattern = r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+trycloudflare\.com'
    if parsed.scheme != 'https' or not re.fullmatch(pattern, host) or parsed.netloc.lower() != host or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('Expected an exact HTTPS https://<name>.trycloudflare.com origin')
    return 'https://' + host


def write_origin(value: str) -> str:
    origin = validate_public_origin(value)
    ensure_layout()
    tmp = ORIGIN_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps({'origin': origin, 'updated_at': time.time()}, indent=2), encoding='utf-8')
    tmp.replace(ORIGIN_PATH)
    return origin


_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[tuple[str, str], deque] = defaultdict(deque)
_UPLOAD_WRITE_LOCK = threading.RLock()


def rate_allow(bucket: str, key: str, limit: int, window_seconds: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with _RATE_LOCK:
        q = _RATE_BUCKETS[(bucket, key)]
        while q and now - q[0] >= window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(_RATE_BUCKETS) > 10000:
            for k, v in list(_RATE_BUCKETS.items()):
                while v and now - v[0] >= window_seconds:
                    v.popleft()
                if not v:
                    _RATE_BUCKETS.pop(k, None)
        return True


def rate_retry_after(bucket: str, key: str, window_seconds: int) -> int:
    now = time.monotonic()
    with _RATE_LOCK:
        q = _RATE_BUCKETS.get((bucket, key), ())
        return max(1, math.ceil(window_seconds - (now - q[0]))) if q else window_seconds


def enforce_job_quotas(db: sqlite3.Connection, session_id: str, ip_hash: str, now: float) -> None:
    windows = (
        ('session_id=?', session_id, MAX_SESSION_JOBS_HOUR, 3600, 'Đã đạt giới hạn 2 job mỗi phiên trong một giờ.'),
        ('json_extract(data,\'$.client_ip_hash\')=?', ip_hash, MAX_IP_JOBS_HOUR, 3600, 'Địa chỉ mạng này đã đạt giới hạn 2 job trong một giờ.'),
        ('1=1', None, MAX_GLOBAL_JOBS_HOUR, 3600, 'Máy chủ đã đạt giới hạn job trong một giờ.'),
        ('session_id=?', session_id, MAX_SESSION_JOBS_DAY, 24 * 3600, 'Đã đạt giới hạn 8 job mỗi phiên trong 24 giờ.'),
        ('json_extract(data,\'$.client_ip_hash\')=?', ip_hash, MAX_IP_JOBS_DAY, 24 * 3600, 'Địa chỉ mạng này đã đạt giới hạn 8 job trong 24 giờ.'),
        ('1=1', None, MAX_GLOBAL_JOBS_DAY, 24 * 3600, 'Máy chủ đã đạt giới hạn job trong 24 giờ.'),
    )
    for condition, value, limit, window, message in windows:
        params = [now - window]
        if value is not None:
            params.insert(0, value)
        row = db.execute(
            f'SELECT COUNT(*) AS n, MIN(created) AS oldest FROM jobs WHERE {condition} AND created>? ',
            params,
        ).fetchone()
        if int(row['n'] or 0) >= limit:
            wait = max(1, math.ceil(float(row['oldest']) + window - now))
            raise PublicError(message, 429, 'job_rate_limited', wait)


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()


def safe_filename(value: str) -> str:
    value = str(value or 'video.mp4').replace('\\', '/')
    name = value.rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip()[:160]
    suffix = pathlib.Path(name).suffix.lower()
    if suffix not in SAFE_EXTENSIONS:
        raise PublicError('Upload video MP4, MOV, MKV hoặc WEBM.', 415, 'unsupported_video_type')
    stem = pathlib.Path(name).stem
    stem = re.sub(r'[^\w .()-]+', '_', stem, flags=re.UNICODE).strip(' .')[:80] or 'video'
    return stem + suffix


def safe_event(job_id: str, message: str) -> None:
    allowed = {
        'Đã nhận video.', 'Bắt đầu xử lý video.', 'Đang nhận diện lời thoại.',
        'Đang dịch phụ đề.', 'Đang tạo phụ đề.', 'Đang tạo giọng đọc tiếng Việt.',
        'Đang ghép video.', 'Đang xử lý video.', 'Job hoàn tất.',
        'Job đã dừng vì vượt giới hạn tài nguyên.', 'Job bị lỗi; chi tiết nội bộ được giữ riêng.',
        'Đang dừng job theo yêu cầu của khách.', 'Job đã bị hủy.',
    }
    if message not in allowed:
        message = 'Đang xử lý video.'
    with db_connect() as db:
        db.execute('INSERT INTO events(job_id,message,created) VALUES(?,?,?)', (job_id, message, time.time()))
        db.execute('DELETE FROM events WHERE job_id=? AND id NOT IN (SELECT id FROM events WHERE job_id=? ORDER BY id DESC LIMIT 200)', (job_id, job_id))


def public_job(job: dict) -> dict:
    stage = job.get('current_stage')
    queue_position = None
    if job.get('status') == 'queued':
        with db_connect() as db:
            queue_position = 1 + int(db.execute(
                "SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.status')='queued' AND (created<? OR (created=? AND id<?))",
                (job.get('created_at', 0), job.get('created_at', 0), job['id']),
            ).fetchone()[0])
    wait_seconds, wait_samples, wait_basis = estimate_queue_wait_seconds(job) if queue_position else (None, 0, 'not_queued')
    processing_seconds, processing_samples = processing_time_estimate(float(job.get('duration') or MAX_VIDEO_SECONDS))
    confidence_samples = min((count for count in (wait_samples, processing_samples) if count > 0), default=0)
    return {
        'id': job['id'],
        'title': job.get('title') or 'Video',
        'status': job.get('status'),
        'queue_position': queue_position,
        'estimated_wait_seconds': wait_seconds,
        'estimated_wait_basis': wait_basis,
        'estimated_processing_seconds': processing_seconds,
        'estimate_sample_count': confidence_samples,
        'current_stage': stage,
        'stage_label': LABELS.get(stage, '') if stage else '',
        'stages': {s: job.get('stages', {}).get(s, 'waiting') for s in STAGES},
        'duration': job.get('duration'),
        'progress': int(job.get('progress') or 0),
        'sentences': int(job.get('sentences') or 0),
        'chunks': int(job.get('chunks') or 0),
        'error': job.get('error'),
        'created_at': job.get('created_at'),
        'updated_at': job.get('updated_at'),
        'output_ready': bool(job.get('status') == 'completed' and (JOBS_DIR / job['id'] / 'output.mp4').is_file()),
        'subtitles_ready': bool(job.get('status') == 'completed' and (JOBS_DIR / job['id'] / 'subtitles.srt').is_file()),
        'can_cancel': job.get('status') in ('queued', 'running'),
    }


def get_job(job_id: str) -> dict | None:
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return None
    with db_connect() as db:
        row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
    return json.loads(row['data']) if row else None


def get_owned_job(job_id: str, session_id: str) -> dict | None:
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return None
    with db_connect() as db:
        row = db.execute('SELECT data FROM jobs WHERE id=? AND session_id=?', (job_id, session_id)).fetchone()
    return json.loads(row['data']) if row else None


def save_job(job: dict) -> None:
    job['updated_at'] = time.time()
    with db_connect() as db:
        db.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['updated_at'], job['id']))


def probe_public_video(path: pathlib.Path) -> float:
    path = path.resolve(strict=True)
    if not path.is_relative_to(UPLOADS_DIR.resolve()) and not path.is_relative_to(JOBS_DIR.resolve()):
        raise PublicError('Không đọc được video đã tải lên.', 400, 'invalid_upload')
    if not path.is_file() or path.is_symlink() or path.stat().st_size < 64 * 1024 or path.stat().st_size > MAX_UPLOAD_HARD_CAP_BYTES:
        raise PublicError('Dung lượng video không hợp lệ.', 413, 'invalid_upload_size')
    result = subprocess.run(
        [PUBLIC_CFG['ffprobe'], '-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(path)],
        capture_output=True, text=True, errors='replace', timeout=20,
    )
    if result.returncode:
        raise PublicError('Không đọc được định dạng video.', 415, 'invalid_video')
    try:
        obj = json.loads(result.stdout)
        duration = float(obj['format']['duration'])
        kinds = {stream.get('codec_type') for stream in obj.get('streams', [])}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise PublicError('Không đọc được thời lượng video.', 415, 'invalid_video')
    if not math.isfinite(duration) or duration < MIN_VIDEO_SECONDS or duration > MAX_VIDEO_SECONDS:
        raise PublicError('Video phải dài từ 30 giây đến tối đa 2 giờ.', 413, 'video_duration_limit')
    if 'video' not in kinds or 'audio' not in kinds:
        raise PublicError('Video cần có cả hình và âm thanh.', 415, 'video_stream_required')
    return duration


def current_ip_hash(headers, peer_ip: str, is_cloudflare: bool) -> str:
    candidate = headers.get('CF-Connecting-IP', '') if is_cloudflare else peer_ip
    try:
        normalized = str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        normalized = str(ipaddress.ip_address(peer_ip))
    return hash_value(normalized)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PublicDemo'
    sys_version = ''

    def log_message(self, *_args):
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def _json(self, value: dict | list, status: int = 200, extra: dict | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if getattr(self, '_secure_context', False):
            self.send_header('Strict-Transport-Security', 'max-age=86400')
        if getattr(self, '_pending_cookie', None):
            self.send_header('Set-Cookie', self._pending_cookie)
            self._pending_cookie = None
        if extra:
            for key, val in extra.items():
                self.send_header(key, val)
        self.end_headers()
        self.wfile.write(payload)

    def _fail(self, message: str, status: int = 400, code: str = 'invalid_request', retry_after: int | None = None) -> None:
        payload = {'error': message, 'code': code}
        extra = None
        if retry_after is not None:
            retry_after = max(1, min(int(retry_after), 24 * 3600))
            payload['retry_after'] = retry_after
            extra = {'Retry-After': str(retry_after)}
        self._json(payload, status, extra)

    def _context(self) -> dict | None:
        host = self.headers.get('Host', '').strip().lower()
        configured_origin = origin_from_file()
        configured_host = urlsplit(configured_origin).hostname if configured_origin else None
        cf_ip = self.headers.get('CF-Connecting-IP', '')
        cf_ray = self.headers.get('CF-Ray', '')
        try:
            visitor = json.loads(self.headers.get('CF-Visitor', '{}'))
        except (TypeError, ValueError):
            visitor = {}
        try:
            cf_client = ipaddress.ip_address(cf_ip.strip())
            cf_ip_ok = cf_client.is_global
        except ValueError:
            cf_ip_ok = False
        try:
            peer_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            peer_loopback = False
        ray_ok = bool(re.fullmatch(r'[a-f0-9]{16,32}-[a-z]{3}', cf_ray.lower()))
        cf_verified = bool(configured_origin and peer_loopback and cf_ip_ok and ray_ok and visitor.get('scheme') == 'https')
        port = int(self.server.server_address[1])
        local_hosts = {f'127.0.0.1:{port}', f'localhost:{port}', '127.0.0.1', 'localhost'}
        if configured_host and host == configured_host and cf_verified:
            public = True
        elif host in local_hosts and peer_loopback and not (cf_ip or cf_ray or visitor):
            public = False
        else:
            self._fail('Host không được cấu hình.', 421, 'invalid_host')
            return None
        self._secure_context = public
        peer = self.client_address[0]
        self._client_ip_hash = current_ip_hash(self.headers, peer, public)
        self._request_origin = configured_origin if public else ''
        if not rate_allow('request-global', 'all', 3000, 60):
            self._fail('Tạm giới hạn lưu lượng.', 429, 'rate_limited', 60)
            return None
        if not rate_allow('request-ip', self._client_ip_hash, 240, 60):
            self._fail('Tạm giới hạn lưu lượng từ mạng này.', 429, 'rate_limited', 60)
            return None
        return {'public': public, 'host': host, 'origin': configured_origin}

    def _check_post_origin(self, ctx: dict) -> bool:
        origin = self.headers.get('Origin', '')
        if ctx['public']:
            expected = ctx['origin']
        else:
            expected = 'http://' + ctx['host']
        if not origin or origin != expected:
            self._fail('Origin không hợp lệ.', 403, 'origin_rejected')
            return False
        return True

    def _read_body(self, limit: int) -> bytes:
        if self.headers.get('Transfer-Encoding'):
            raise PublicError('Không hỗ trợ dạng truyền tải này.', 411, 'length_required')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise PublicError('Content-Length không hợp lệ.', 400, 'invalid_content_length')
        if size < 0 or size > limit:
            self.close_connection = True
            raise PublicError('Dữ liệu vượt giới hạn.', 413, 'request_too_large')
        try:
            data = self.rfile.read(size) if size else b''
        except socket.timeout:
            self.close_connection = True
            raise PublicError('Upload bị ngắt giữa chừng.', 408, 'incomplete_body')
        if len(data) != size:
            raise PublicError('Upload bị ngắt giữa chừng.', 400, 'incomplete_body')
        return data

    def _body_json(self, limit: int = 16 * 1024) -> dict:
        raw = self._read_body(limit)
        try:
            value = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PublicError('JSON không hợp lệ.', 400, 'invalid_json')
        if not isinstance(value, dict):
            raise PublicError('JSON phải là object.', 400, 'invalid_json')
        return value

    def _session(self) -> str:
        token = ''
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            token = cookies[SESSION_COOKIE].value if SESSION_COOKIE in cookies else ''
        except Exception:
            token = ''
        if re.fullmatch(r'[A-Za-z0-9_-]{40,48}', token):
            session_id = hash_value(token)
            with db_connect() as db:
                row = db.execute('SELECT id_hash FROM sessions WHERE id_hash=? AND last_seen>?', (session_id, time.time() - SESSION_TTL_SECONDS)).fetchone()
                if row:
                    db.execute('UPDATE sessions SET last_seen=?,ip_hash=? WHERE id_hash=?', (time.time(), self._client_ip_hash, session_id))
                    return session_id
        if not rate_allow('new-session-ip', self._client_ip_hash, 12, 3600):
            raise PublicError('Không thể tạo thêm phiên demo từ mạng này lúc này.', 429, 'session_rate_limited', rate_retry_after('new-session-ip', self._client_ip_hash, 3600))
        if not rate_allow('new-session-global', 'all', 120, 3600):
            raise PublicError('Máy chủ đã đạt giới hạn tạo phiên demo.', 429, 'session_rate_limited', rate_retry_after('new-session-global', 'all', 3600))
        token = secrets.token_urlsafe(32)
        session_id = hash_value(token)
        now = time.time()
        with db_connect() as db:
            db.execute('INSERT OR REPLACE INTO sessions(id_hash,ip_hash,created,last_seen) VALUES(?,?,?,?)', (session_id, self._client_ip_hash, now, now))
        attrs = f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}'
        if getattr(self, '_secure_context', False):
            attrs += '; Secure'
        self._pending_cookie = attrs
        return session_id

    def _session_rate(self, session_id: str, bucket: str, limit: int, seconds: int) -> None:
        if not rate_allow(bucket, session_id, limit, seconds):
            raise PublicError('Đã đạt giới hạn thao tác demo. Vui lòng thử lại sau.', 429, 'rate_limited', rate_retry_after(bucket, session_id, seconds))

    def _session_jobs(self, session_id: str) -> list[dict]:
        with db_connect() as db:
            rows = db.execute('SELECT data FROM jobs WHERE session_id=? ORDER BY created DESC LIMIT 10', (session_id,)).fetchall()
        return [public_job(json.loads(row['data'])) for row in rows]

    def _cancel_job(self, session_id: str, job_id: str):
        with db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM jobs WHERE id=? AND session_id=?', (job_id, session_id)).fetchone()
            if not row:
                raise PublicError('Không tìm thấy job.', 404, 'job_not_found')
            job = json.loads(row['data'])
            status = job.get('status')
            if status == 'queued':
                job['status'] = 'cancelled'
                job['error'] = 'JOB_STOPPED'
                job['current_stage'] = None
            elif status == 'running':
                job['status'] = 'cancel_requested'
            elif status == 'cancel_requested':
                return self._json(public_job(job), 202)
            else:
                raise PublicError('Job đã kết thúc và không thể hủy.', 409, 'job_not_running')
            job['updated_at'] = time.time()
            db.execute('UPDATE jobs SET data=?,updated=? WHERE id=? AND session_id=?', (json.dumps(job, ensure_ascii=False), job['updated_at'], job_id, session_id))
        if status == 'queued':
            _cleanup_job_temporary_files(job)
            safe_event(job_id, 'Job đã bị hủy.')
            return self._json(public_job(get_owned_job(job_id, session_id) or job), 200)
        safe_event(job_id, 'Đang dừng job theo yêu cầu của khách.')
        return self._json(public_job(job), 202)

    def _delete_job(self, session_id: str, job_id: str):
        upload_path = ''
        with db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM jobs WHERE id=? AND session_id=?', (job_id, session_id)).fetchone()
            if not row:
                raise PublicError('Không tìm thấy job.', 404, 'job_not_found')
            job = json.loads(row['data'])
            if job.get('status') in ('running', 'queued', 'cancel_requested'):
                raise PublicError('Hãy hủy job đang hoạt động trước khi xóa.', 409, 'job_still_active')
            upload_path = str(job.get('upload_path') or '')
            db.execute('DELETE FROM events WHERE job_id=?', (job_id,))
            db.execute('DELETE FROM jobs WHERE id=? AND session_id=?', (job_id, session_id))
        _remove_public_job(job_id)
        try:
            upload = pathlib.Path(upload_path).resolve(strict=True)
            if upload.parent == UPLOADS_DIR.resolve() and upload.is_file() and not upload.is_symlink():
                upload.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            pass
        return self._json({'deleted': True})

    def _send_file(self, file: pathlib.Path, download: bool = False) -> None:
        try:
            resolved = file.resolve(strict=True)
            if not resolved.is_relative_to(JOBS_DIR.resolve()) or not resolved.is_file():
                raise OSError('not a public job file')
            size = resolved.stat().st_size
        except OSError:
            self._fail('Output chưa có hoặc đã hết hạn.', 404, 'output_missing')
            return
        if size <= 0:
            self._fail('Output chưa có.', 404, 'output_missing')
            return
        start, end = 0, size - 1
        range_header = self.headers.get('Range', '')
        status = 200
        if range_header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = min(size - 1, int(match.group(2))) if match.group(2) else size - 1
            else:
                count = int(match.group(2))
                start = max(0, size - count)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        media_type = mimetypes.guess_type(resolved.name)[0] or 'application/octet-stream'
        self.send_response(status)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Disposition', 'attachment; filename="output.mp4"' if download else 'inline')
        if range_header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        try:
            with resolved.open('rb') as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_OPTIONS(self):
        if self._context() is None:
            return
        self._fail('Cross-origin requests are disabled.', 403, 'cors_disabled')

    def do_GET(self):
        ctx = self._context()
        if ctx is None:
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/public/health':
                return self._json(pipeline_health())
            if path == '/api/public/session':
                self._session()
                return self._json({'ok': True})
            if path == '/api/public/state':
                session_id = self._session()
                self._session_rate(session_id, 'session-requests', 120, 60)
                return self._json({'jobs': self._session_jobs(session_id), 'health': pipeline_health()})
            upload_match = re.fullmatch(r'/api/public/uploads/([a-f0-9]{32})', path)
            if upload_match:
                session_id = self._session()
                self._session_rate(session_id, 'session-requests', 120, 60)
                with db_connect() as db:
                    upload = db.execute(
                        'SELECT expected_size,received FROM uploads WHERE id=? AND session_id=? AND consumed=0',
                        (upload_match.group(1), session_id),
                    ).fetchone()
                if not upload:
                    return self._fail('Không tìm thấy upload đang chờ.', 404, 'upload_not_found')
                return self._json({'received': int(upload['received']), 'size': int(upload['expected_size'])})
            match = re.fullmatch(r'/api/public/jobs/([a-f0-9]{32})(?:/(log|segments|output|subtitles|vtt))?', path)
            if match:
                session_id = self._session()
                self._session_rate(session_id, 'session-requests', 120, 60)
                job = get_owned_job(match.group(1), session_id)
                if not job:
                    return self._fail('Không tìm thấy job.', 404, 'job_not_found')
                asset = match.group(2)
                if not asset:
                    return self._json(public_job(job))
                if asset == 'log':
                    with db_connect() as db:
                        rows = db.execute('SELECT message,created FROM events WHERE job_id=? ORDER BY id DESC LIMIT 100', (job['id'],)).fetchall()
                    return self._json({'events': [{'message': row['message'], 'created_at': row['created']} for row in reversed(rows)]})
                folder = JOBS_DIR / job['id']
                if asset == 'segments':
                    rows = read_json(folder / 'segments.json', [])
                    safe_rows = [{'start': r.get('start'), 'end': r.get('end'), 'target': str(r.get('target', ''))[:1200]} for r in rows if isinstance(r, dict)]
                    return self._json(safe_rows)
                if asset in ('output', 'subtitles', 'vtt'):
                    if asset == 'output':
                        if ctx['public'] and not MP4_DELIVERY_VIA_TUNNEL:
                            raise PublicError('MP4 preview, Range, and download are disabled through the no-cost Quick Tunnel. Use a video-hosting service that permits public delivery.', 403, 'video_delivery_unavailable')
                        self._session_rate(session_id, 'output-reads-session', 240, 3600)
                        if urlsplit(self.path).query == 'download=1':
                            self._session_rate(session_id, 'output-downloads-session', 8, 3600)
                            if not rate_allow('output-downloads-ip', self._client_ip_hash, 24, 3600):
                                raise PublicError('Đã đạt giới hạn tải output trong một giờ.', 429, 'download_rate_limited')
                    name = {'output': 'output.mp4', 'subtitles': 'subtitles.srt', 'vtt': 'subtitles.vtt'}[asset]
                    return self._send_file(folder / name, download=urlsplit(self.path).query == 'download=1')
            if path in ('/', '/index.html', '/app.js', '/style.css'):
                if path in ('/', '/index.html'):
                    self._session()
                    filename = 'index.html'
                else:
                    filename = path.lstrip('/')
                file = PUBLIC_DIST / filename
                if not file.is_file() or not file.resolve().is_relative_to(PUBLIC_DIST.resolve()):
                    return self._fail('Không tìm thấy.', 404, 'not_found')
                payload = file.read_bytes()
                media = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                self.send_response(200)
                self.send_header('Content-Type', media + ('; charset=utf-8' if media.startswith('text/') or 'javascript' in media else ''))
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store, max-age=0')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                if getattr(self, '_secure_context', False):
                    self.send_header('Strict-Transport-Security', 'max-age=86400')
                if getattr(self, '_pending_cookie', None):
                    self.send_header('Set-Cookie', self._pending_cookie)
                    self._pending_cookie = None
                self.end_headers()
                self.wfile.write(payload)
                return
            return self._fail('Không tìm thấy.', 404, 'not_found')
        except PublicError as exc:
            return self._fail(str(exc), exc.status, exc.code, exc.retry_after)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            return self._fail('Không thể xử lý yêu cầu.', 500, 'internal_error')

    def do_POST(self):
        ctx = self._context()
        if ctx is None or not self._check_post_origin(ctx):
            return
        try:
            session_id = self._session()
            self._session_rate(session_id, 'session-requests', 120, 60)
            path = urlsplit(self.path).path
            if path == '/api/public/uploads/start':
                self._session_rate(session_id, 'upload-start-session', 5, 3600)
                return self._start_upload(session_id)
            match = re.fullmatch(r'/api/public/uploads/([a-f0-9]{32})', path)
            if match:
                self._session_rate(session_id, 'upload-chunk-session', 1200, 3600)
                return self._write_chunk(session_id, match.group(1))
            if path == '/api/public/jobs':
                self._session_rate(session_id, 'job-create-session', 3, 24 * 3600)
                return self._create_job(session_id)
            match = re.fullmatch(r'/api/public/jobs/([a-f0-9]{32})/cancel', path)
            if match:
                self._session_rate(session_id, 'job-cancel-session', 12, 3600)
                return self._cancel_job(session_id, match.group(1))
            match = re.fullmatch(r'/api/public/jobs/([a-f0-9]{32})/delete', path)
            if match:
                self._session_rate(session_id, 'job-delete-session', 12, 3600)
                return self._delete_job(session_id, match.group(1))
            return self._fail('Không tìm thấy.', 404, 'not_found')
        except PublicError as exc:
            return self._fail(str(exc), exc.status, exc.code, exc.retry_after)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            return self._fail('Không thể xử lý yêu cầu.', 500, 'internal_error')

    def _start_upload(self, session_id: str):
        with _UPLOAD_WRITE_LOCK:
            return self._start_upload_locked(session_id)

    def _start_upload_locked(self, session_id: str):
        health = pipeline_health(force=True)
        if not health['ready']:
            raise PublicError('Máy chủ đang thiếu model hoặc tài nguyên an toàn; chưa nhận upload mới.', 503, 'pipeline_unavailable')
        body = self._body_json()
        filename = safe_filename(body.get('name', 'video.mp4'))
        fingerprint = str(body.get('fingerprint') or '')
        if fingerprint and not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
            raise PublicError('Dấu nhận dạng file không hợp lệ.', 400, 'invalid_upload_fingerprint')
        try:
            size = int(body.get('size'))
        except (TypeError, ValueError):
            raise PublicError('Dung lượng file không hợp lệ.', 400, 'invalid_upload_size')
        if size < 64 * 1024 or size > MAX_UPLOAD_HARD_CAP_BYTES:
            raise PublicError('File phải từ 64 KiB đến tối đa 8 GiB.', 413, 'upload_size_limit')
        now = time.time()
        upload_id = uuid.uuid4().hex
        stored_name = upload_id + pathlib.Path(filename).suffix.lower()
        with db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute(
                'SELECT * FROM uploads WHERE session_id=? AND consumed=0 AND created>? ORDER BY created DESC LIMIT 1',
                (session_id, now - UPLOAD_TTL_SECONDS),
            ).fetchone()
            if pending:
                target = UPLOADS_DIR / pending['stored_name']
                if target.is_file() and not target.is_symlink() and int(pending['received']) <= target.stat().st_size <= int(pending['received']) + MAX_CHUNK_BYTES:
                    with target.open('r+b') as stream:
                        stream.truncate(int(pending['received']))
                resumable = (
                    pending['filename'] == filename
                    and int(pending['expected_size']) == size
                    and pending['fingerprint'] == fingerprint
                    and target.is_file() and not target.is_symlink()
                    and target.stat().st_size == int(pending['received'])
                )
                if not resumable:
                    raise PublicError('Có upload chưa xong. Chọn lại cùng file để tiếp tục hoặc đợi 24 giờ.', 409, 'upload_already_open')
                return self._json({
                    'upload': pending['id'], 'chunk_bytes': MAX_CHUNK_BYTES, 'size': size,
                    'received': int(pending['received']), 'resumed': True,
                })
            upload_limit = max_upload_bytes(db)
            if size > upload_limit:
                raise PublicError(f'Dung lượng tối đa hiện tại là {upload_limit} byte; giới hạn được tính theo dung lượng đĩa và hàng đợi.', 413, 'upload_size_limit')
            try:
                free = shutil.disk_usage(PUBLIC_ROOT).free
            except OSError:
                free = 0
            available = free - MIN_FREE_DISK_MIB * 1024 * 1024 - reserved_job_storage(db) - pending_upload_storage(db, now)
            if estimated_job_storage(size) > max(0, available):
                raise PublicError('Đĩa không còn đủ chỗ an toàn cho file và các file trung gian/output dự kiến.', 507, 'public_storage_full')
            total_reserved = db.execute('SELECT COALESCE(SUM(expected_size-received),0) FROM uploads WHERE consumed=0 AND created>? ', (now - UPLOAD_TTL_SECONDS,)).fetchone()[0]
            if total_reserved + size > 2 * MAX_UPLOAD_HARD_CAP_BYTES:
                raise PublicError('Đã đạt giới hạn upload đồng thời.', 429, 'upload_capacity_full')
            by_ip = db.execute('SELECT COUNT(*) FROM uploads u JOIN sessions s ON s.id_hash=u.session_id WHERE s.ip_hash=? AND u.consumed=0 AND u.created>?', (self._client_ip_hash, now - UPLOAD_TTL_SECONDS)).fetchone()[0]
            if by_ip >= 2:
                raise PublicError('Đã đạt giới hạn upload từ mạng này.', 429, 'upload_ip_limit')
            db.execute('INSERT INTO uploads(id,session_id,filename,expected_size,received,stored_name,created,updated,consumed,fingerprint) VALUES(?,?,?,?,?,?,?,?,0,?)', (upload_id, session_id, filename, size, 0, stored_name, now, now, fingerprint))
        try:
            (UPLOADS_DIR / stored_name).touch(exist_ok=False)
        except OSError:
            with db_connect() as db:
                db.execute('DELETE FROM uploads WHERE id=? AND session_id=?', (upload_id, session_id))
            raise PublicError('Không tạo được file upload tạm.', 507, 'upload_storage_failed')
        return self._json({'upload': upload_id, 'chunk_bytes': MAX_CHUNK_BYTES, 'size': size, 'received': 0, 'resumed': False}, 201)

    def _write_chunk(self, session_id: str, upload_id: str):
        with _UPLOAD_WRITE_LOCK:
            return self._write_chunk_locked(session_id, upload_id)

    def _write_chunk_locked(self, session_id: str, upload_id: str):
        with db_connect() as db:
            row = db.execute('SELECT * FROM uploads WHERE id=? AND session_id=? AND consumed=0', (upload_id, session_id)).fetchone()
            if not row:
                raise PublicError('Upload không tồn tại.', 404, 'upload_not_found')
            expected_offset = int(row['received'])
            total = int(row['expected_size'])
            stored_name = row['stored_name']
        try:
            size = int(self.headers.get('Content-Length', '0'))
            offset = int(self.headers.get('X-Offset', '-1'))
        except ValueError:
            raise PublicError('Offset hoặc Content-Length không hợp lệ.', 400, 'invalid_chunk')
        if size < 1 or size > MAX_CHUNK_BYTES or offset != expected_offset or offset + size > total:
            self.close_connection = True
            raise PublicError('Chunk upload không hợp lệ.', 413 if size > MAX_CHUNK_BYTES else 400, 'invalid_chunk')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
            self.close_connection = True
            raise PublicError('Content-Type không hợp lệ.', 415, 'invalid_content_type')
        payload = self._read_body(MAX_CHUNK_BYTES)
        target = UPLOADS_DIR / stored_name
        with open(target, 'r+b') as stream:
            stream.truncate(expected_offset)
            stream.seek(expected_offset)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        now = time.time()
        with db_connect() as db:
            updated = db.execute('UPDATE uploads SET received=received+?,updated=? WHERE id=? AND session_id=? AND consumed=0 AND received=?', (size, now, upload_id, session_id, expected_offset)).rowcount
        if updated != 1:
            with open(target, 'r+b') as stream:
                stream.truncate(expected_offset)
            raise PublicError('Upload thay đổi đồng thời; hãy gửi lại chunk.', 409, 'upload_race')
        return self._json({'received': expected_offset + size, 'complete': expected_offset + size == total})

    def _create_job(self, session_id: str):
        body = self._body_json()
        if 'url' in body or 'link' in body:
            raise PublicError('API công khai chỉ nhận upload file; gửi URL không được hỗ trợ.', 400, 'unsupported_source')
        upload_id = str(body.get('upload') or '')
        if not re.fullmatch(r'[a-f0-9]{32}', upload_id):
            raise PublicError('Upload không hợp lệ.', 400, 'invalid_upload')
        safe = pipeline_health(force=True)
        if not safe['ready']:
            raise PublicError('Pipeline hoặc tài nguyên máy chủ chưa sẵn sàng.', 503, 'pipeline_unavailable')
        with db_connect() as db:
            upload = db.execute('SELECT * FROM uploads WHERE id=? AND session_id=? AND consumed=0', (upload_id, session_id)).fetchone()
        if not upload or int(upload['received']) != int(upload['expected_size']):
            raise PublicError('Upload chưa hoàn tất.', 409, 'upload_incomplete')
        with db_connect() as db:
            running = db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested')").fetchone()[0]
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.status')='queued'").fetchone()[0]
            if running > MAX_ACTIVE_JOBS or queued >= MAX_WAITING_JOBS:
                raise PublicError('Hàng đợi đã có một job chạy và đủ mười job chờ.', 429, 'public_queue_full', 60)
        file = UPLOADS_DIR / upload['stored_name']
        duration = probe_public_video(file)
        source_size = file.stat().st_size
        with db_connect() as db:
            available = available_storage_bytes(db, exclude_upload=upload_id)
        storage_limit = min(MAX_JOB_STORAGE_BYTES, max(0, available))
        required_storage = estimated_job_storage(source_size, duration)
        if required_storage > storage_limit:
            raise PublicError('Video đã được kiểm tra nhưng vượt ngân sách dung lượng còn trống an toàn của máy.', 507, 'public_storage_full')
        now = time.time()
        job_id = uuid.uuid4().hex
        folder = JOBS_DIR / job_id
        title = pathlib.Path(upload['filename']).stem[:80]
        job = {
            'id': job_id, 'title': title, 'session_id': session_id, 'status': 'queued',
            'client_ip_hash': self._client_ip_hash,
            'current_stage': None, 'stages': {stage: 'waiting' for stage in STAGES},
            'stage_times': {}, 'duration': round(duration, 3), 'progress': 0,
            'sentences': 0, 'chunks': 0, 'created_at': now, 'updated_at': now,
            'upload_path': str(file), 'source_path': '', 'output_path': '',
            'storage_limit_bytes': storage_limit,
            'runtime_limit_seconds': job_runtime_limit(duration),
            'error': None,
        }
        folder.mkdir(parents=True, exist_ok=False)
        try:
            with db_connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current_upload = db.execute('SELECT received,expected_size,consumed FROM uploads WHERE id=? AND session_id=?', (upload_id, session_id)).fetchone()
                if not current_upload or current_upload['consumed'] or int(current_upload['received']) != int(current_upload['expected_size']):
                    raise PublicError('Upload đã được sử dụng hoặc chưa hoàn tất.', 409, 'upload_incomplete')
                running = db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested')").fetchone()[0]
                queued = db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.status')='queued'").fetchone()[0]
                if running > MAX_ACTIVE_JOBS or queued >= MAX_WAITING_JOBS:
                    raise PublicError('Hàng đợi đã có một job chạy và đủ mười job chờ.', 429, 'public_queue_full', 60)
                pending_storage = pending_upload_storage(db, now, upload_id)
                if data_size() + reserved_job_storage(db) + pending_storage + required_storage > public_storage_budget_bytes():
                    raise PublicError('Kho demo không còn đủ hạn mức output cho job mới.', 507, 'public_storage_full')
                existing = db.execute("SELECT COUNT(*) FROM jobs WHERE session_id=? AND json_extract(data,'$.status') IN ('running','cancel_requested','queued')", (session_id,)).fetchone()[0]
                if existing:
                    raise PublicError('Phiên này đã có job đang chạy hoặc chờ.', 429, 'session_job_limit', 60)
                enforce_job_quotas(db, session_id, self._client_ip_hash, now)
                db.execute('INSERT INTO jobs(id,session_id,data,created,updated) VALUES(?,?,?,?,?)', (job_id, session_id, json.dumps(job, ensure_ascii=False), now, now))
                consumed = db.execute('UPDATE uploads SET consumed=1,updated=? WHERE id=? AND session_id=? AND consumed=0', (now, upload_id, session_id)).rowcount
                if consumed != 1:
                    raise PublicError('Upload đã được sử dụng.', 409, 'upload_already_consumed')
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        safe_event(job_id, 'Đã nhận video.')
        return self._json(public_job(job), 201)

    def do_PUT(self):
        if self._context() is not None:
            self._fail('Method không được hỗ trợ.', 405, 'method_not_allowed')

    def do_DELETE(self):
        if self._context() is not None:
            self._fail('Method không được hỗ trợ.', 405, 'method_not_allowed')


class JobCancel(threading.Event):
    def __init__(self):
        super().__init__()
        self.proc = None

    def stop(self):
        self.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _update_worker_job(job: dict) -> None:
    save_job(job)


def _safe_engine_log(job_id: str, message: str) -> None:
    message = str(message)
    if message.startswith('Đã nhận diện đoạn'):
        safe_event(job_id, 'Đang nhận diện lời thoại.')
    elif message.startswith('Đã dịch '):
        safe_event(job_id, 'Đang dịch phụ đề.')
    elif message.startswith('Giọng đọc '):
        safe_event(job_id, 'Đang tạo giọng đọc tiếng Việt.')
    elif 'GPU không đạt' in message:
        safe_event(job_id, 'Đang xử lý video.')


def run_worker(job_id: str) -> int:
    from engine import process_stage
    job = get_job(job_id)
    if not job or job.get('status') not in ('running', 'queued'):
        return 2
    safe = pipeline_health(force=True)
    if not safe['ready']:
        job['status'] = 'blocked'
        job['error'] = 'PIPELINE_UNAVAILABLE'
        job['current_stage'] = None
        _update_worker_job(job)
        safe_event(job_id, 'Job bị lỗi; chi tiết nội bộ được giữ riêng.')
        _cleanup_job_temporary_files(job)
        return 1
    folder = JOBS_DIR / job_id
    cancel = JobCancel()
    try:
        for index, stage in enumerate(STAGES):
            if cancel.is_set():
                raise InterruptedError()
            job = get_job(job_id)
            if job.get('status') == 'cancel_requested':
                raise InterruptedError()
            job['status'] = 'running'
            job['current_stage'] = stage
            job['stages'][stage] = 'running'
            _update_worker_job(job)
            if index == 0:
                safe_event(job_id, 'Bắt đầu xử lý video.')
            elif stage == 'transcribe':
                safe_event(job_id, 'Đang nhận diện lời thoại.')
            elif stage == 'translate':
                safe_event(job_id, 'Đang dịch phụ đề.')
            elif stage == 'subtitles':
                safe_event(job_id, 'Đang tạo phụ đề.')
            elif stage == 'tts':
                safe_event(job_id, 'Đang tạo giọng đọc tiếng Việt.')
            elif stage == 'render':
                safe_event(job_id, 'Đang ghép video.')
            started = time.monotonic()
            if stage == 'download' and job.get('source_url'):
                raise PublicError('Job dùng link không còn được hỗ trợ; vui lòng tải file lên.', 400, 'unsupported_source')
            job = process_stage(stage, job, PUBLIC_CFG, PUBLIC_ROOT, lambda msg: _safe_engine_log(job_id, msg), cancel)
            elapsed = round(time.monotonic() - started, 2)
            job['stages'][stage] = 'done'
            job.setdefault('stage_times', {})[stage] = elapsed
            job['current_stage'] = None
            job['progress'] = int((index + 1) * 100 / len(STAGES))
            _update_worker_job(job)
        job = get_job(job_id)
        job['status'] = 'completed'
        job['current_stage'] = None
        job['progress'] = 100
        job['error'] = None
        job['runtime_seconds'] = round(max(0, time.time() - float(job.get('processing_started_at') or job.get('created_at') or time.time())), 1)
        job['output_path'] = str(folder / 'output.mp4')
        _update_worker_job(job)
        safe_event(job_id, 'Job hoàn tất.')
        upload = pathlib.Path(job.get('upload_path') or '')
        if upload.is_file() and upload.resolve().is_relative_to(UPLOADS_DIR.resolve()):
            upload.unlink(missing_ok=True)
        return 0
    except InterruptedError:
        job = get_job(job_id) or job
        job['status'] = 'cancelled'
        job['error'] = 'JOB_STOPPED'
        job['current_stage'] = None
        _update_worker_job(job)
        _cleanup_job_temporary_files(job)
        safe_event(job_id, 'Job đã bị hủy.')
        return 1
    except PublicError as exc:
        job = get_job(job_id) or job
        stage = job.get('current_stage')
        if stage in job.get('stages', {}):
            job['stages'][stage] = 'blocked'
        job['status'] = 'blocked'
        job['error'] = exc.code
        job['current_stage'] = None
        _update_worker_job(job)
        safe_event(job_id, str(exc))
        _cleanup_job_temporary_files(job)
        return 1
    except Exception:
        job = get_job(job_id) or job
        stage = job.get('current_stage')
        if stage in job.get('stages', {}):
            job['stages'][stage] = 'blocked'
        job['status'] = 'blocked'
        job['error'] = 'PIPELINE_FAILED'
        job['current_stage'] = None
        _update_worker_job(job)
        safe_event(job_id, 'Job bị lỗi; chi tiết nội bộ được giữ riêng.')
        _cleanup_job_temporary_files(job)
        return 1


def _kill_worker_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.kill()


class Scheduler:
    def __init__(self):
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.job_id: str | None = None
        self.started = 0.0
        self.runtime_limit = MAX_JOB_SECONDS
        self.storage_limit = MAX_JOB_STORAGE_BYTES
        self.last_resource_check = 0.0
        self.reason = ''

    def start(self):
        threading.Thread(target=self.loop, name='public-demo-scheduler', daemon=True).start()

    def _set_blocked(self, job_id: str, error: str, message: str):
        job = get_job(job_id)
        if not job:
            return
        job['status'] = 'blocked'
        job['error'] = error
        job['current_stage'] = None
        save_job(job)
        safe_event(job_id, message)
        _cleanup_job_temporary_files(job)

    def _launch_next(self):
        running, _ = active_counts()
        if running >= MAX_ACTIVE_JOBS:
            return
        with db_connect() as db:
            row = db.execute("SELECT id,data FROM jobs WHERE json_extract(data,'$.status')='queued' ORDER BY created,id LIMIT 1").fetchone()
        if not row:
            return
        with db_connect() as db:
            if data_size() + reserved_job_storage(db) > public_storage_budget_bytes():
                self._set_blocked(row['id'], 'PUBLIC_STORAGE_LIMIT', 'Job chưa chạy được vì kho demo đã đạt hạn mức an toàn.')
                return
        health = pipeline_health(force=True)
        if not health['ready']:
            self._set_blocked(row['id'], 'PIPELINE_UNAVAILABLE', 'Job chưa chạy được vì máy chủ đang thiếu tài nguyên an toàn.')
            return
        with db_connect() as db:
            db.execute('BEGIN IMMEDIATE')
            running = db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested')").fetchone()[0]
            if running >= MAX_ACTIVE_JOBS:
                return
            row = db.execute("SELECT id,data FROM jobs WHERE json_extract(data,'$.status')='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return
            job = json.loads(row['data'])
            job['status'] = 'running'
            job['processing_started_at'] = time.time()
            job['updated_at'] = time.time()
            db.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['updated_at'], row['id']))
        job_id = row['id']
        env = os.environ.copy()
        env['HF_HUB_OFFLINE'] = '1'
        env['TRANSFORMERS_OFFLINE'] = '1'
        env['HF_HUB_DISABLE_TELEMETRY'] = '1'
        env['TOKENIZERS_PARALLELISM'] = 'false'
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        try:
            proc = subprocess.Popen([sys.executable, str(BASE / 'public_demo.py'), '--worker', job_id], cwd=str(BASE), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError:
            self._set_blocked(job_id, 'WORKER_START_FAILED', 'Job bị lỗi; chi tiết nội bộ được giữ riêng.')
            return
        self.process = proc
        self.job_id = job_id
        self.started = time.monotonic()
        self.runtime_limit = min(MAX_JOB_SECONDS, int(job.get('runtime_limit_seconds') or MAX_JOB_SECONDS))
        self.storage_limit = min(MAX_JOB_STORAGE_BYTES, int(job.get('storage_limit_bytes') or MAX_JOB_STORAGE_BYTES))
        self.last_resource_check = 0.0
        self.reason = ''

    def _monitor(self):
        proc, job_id = self.process, self.job_id
        if not proc or not job_id:
            return
        code = proc.poll()
        if code is not None:
            job = get_job(job_id)
            if job and job.get('status') == 'cancel_requested':
                job['status'] = 'cancelled'
                job['error'] = 'JOB_STOPPED'
                job['current_stage'] = None
                save_job(job)
                _cleanup_job_temporary_files(job)
                safe_event(job_id, 'Job đã bị hủy.')
            elif job and job.get('status') == 'running':
                self._set_blocked(job_id, 'WORKER_FAILED', 'Job bị lỗi; chi tiết nội bộ được giữ riêng.')
            self.process = None
            self.job_id = None
            return
        job = get_job(job_id)
        if job and job.get('status') == 'cancel_requested':
            _kill_worker_tree(proc)
            job['status'] = 'cancelled'
            job['error'] = 'JOB_STOPPED'
            job['current_stage'] = None
            save_job(job)
            _cleanup_job_temporary_files(job)
            safe_event(job_id, 'Job đã bị hủy.')
            self.process = None
            self.job_id = None
            return
        folder_size = data_size(JOBS_DIR / job_id)
        if job:
            try:
                source = pathlib.Path(str(job.get('upload_path') or ''))
                if source.is_file() and source.resolve().is_relative_to(UPLOADS_DIR.resolve()):
                    folder_size += source.stat().st_size
            except (OSError, RuntimeError):
                pass
        if folder_size > self.storage_limit:
            self.reason = 'JOB_STORAGE_LIMIT'
        elif time.monotonic() - self.started > min(MAX_JOB_SECONDS, int((job or {}).get('runtime_limit_seconds') or self.runtime_limit)):
            self.reason = 'JOB_TIMEOUT'
        elif time.monotonic() - self.last_resource_check > 15:
            self.last_resource_check = time.monotonic()
            mem = system_memory() or {}
            if int(mem.get('free_mib') or 0) < 3072:
                self.reason = 'RESOURCE_PRESSURE'
            else:
                try:
                    if shutil.disk_usage(PUBLIC_ROOT).free // (1024 * 1024) < MIN_FREE_DISK_MIB:
                        self.reason = 'RESOURCE_PRESSURE'
                    elif data_size() > public_storage_budget_bytes():
                        self.reason = 'PUBLIC_STORAGE_LIMIT'
                except OSError:
                    self.reason = 'RESOURCE_PRESSURE'
        if self.reason:
            _kill_worker_tree(proc)
            self._set_blocked(job_id, self.reason, 'Job đã dừng vì vượt giới hạn tài nguyên.')
            self.process = None
            self.job_id = None

    def loop(self):
        while True:
            try:
                with self.lock:
                    cleanup_expired()
                    if self.process:
                        self._monitor()
                    else:
                        self._launch_next()
            except Exception:
                pass
            time.sleep(1)


def _remove_public_job(job_id: str) -> None:
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return
    folder = JOBS_DIR / job_id
    try:
        resolved = folder.resolve()
        if resolved.is_relative_to(JOBS_DIR.resolve()) and resolved.parent == JOBS_DIR.resolve():
            shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        pass


def _cleanup_job_temporary_files(job: dict) -> None:
    upload = pathlib.Path(str(job.get('upload_path') or ''))
    try:
        resolved_upload = upload.resolve(strict=True)
        if resolved_upload.parent == UPLOADS_DIR.resolve() and resolved_upload.is_file() and not resolved_upload.is_symlink():
            resolved_upload.unlink(missing_ok=True)
    except (OSError, RuntimeError):
        pass
    _remove_public_job(str(job.get('id') or ''))


def cleanup_expired(now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        with db_connect() as db:
            uploads = db.execute('SELECT id,stored_name FROM uploads WHERE created<?', (now - UPLOAD_TTL_SECONDS,)).fetchall()
            for row in uploads:
                path = UPLOADS_DIR / row['stored_name']
                try:
                    if path.resolve().parent == UPLOADS_DIR.resolve():
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                db.execute('DELETE FROM uploads WHERE id=?', (row['id'],))
            db.execute('DELETE FROM sessions WHERE last_seen<?', (now - 30 * 24 * 3600,))
    except sqlite3.Error:
        return


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound anonymous request threads so slow requests cannot exhaust host memory."""

    def __init__(self, server_address, RequestHandlerClass, *, max_threads: int = 32):
        self.max_threads = max(1, int(max_threads))
        self._request_slots = threading.BoundedSemaphore(self.max_threads)
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\nRetry-After: 5\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def make_server(host: str, port: int, max_threads: int = 32) -> BoundedThreadingHTTPServer:
    server = BoundedThreadingHTTPServer((host, port), Handler, max_threads=max_threads)
    server.daemon_threads = True
    server.request_queue_size = 32
    server.timeout = 1
    try:
        init_db()
        recover_interrupted_jobs()
    except Exception:
        server.server_close()
        raise
    return server


def recover_interrupted_jobs() -> None:
    recovered = []
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT id,data FROM jobs WHERE json_extract(data,'$.status') IN ('running','cancel_requested')").fetchall()
        for row in rows:
            job = json.loads(row['data'])
            cancelled = job.get('status') == 'cancel_requested'
            job['status'] = 'cancelled' if cancelled else 'blocked'
            job['error'] = 'JOB_STOPPED' if cancelled else 'WORKER_INTERRUPTED'
            job['current_stage'] = None
            job['updated_at'] = time.time()
            db.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['updated_at'], row['id']))
            recovered.append(job)
    for job in recovered:
        _cleanup_job_temporary_files(job)
        safe_event(job['id'], 'Job đã bị hủy.' if job['status'] == 'cancelled' else 'Job bị lỗi; chi tiết nội bộ được giữ riêng.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Upload-only public demo service')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--worker')
    parser.add_argument('--set-origin')
    parser.add_argument('--clear-origin', action='store_true')
    args = parser.parse_args()
    if args.set_origin:
        print(write_origin(args.set_origin))
        return 0
    if args.clear_origin:
        ensure_layout()
        ORIGIN_PATH.unlink(missing_ok=True)
        print('Public origin cleared; tunnel requests are rejected until set again.')
        return 0
    if args.worker:
        return run_worker(args.worker)
    if args.host != '127.0.0.1':
        print('PUBLIC_DEMO may bind only to loopback (127.0.0.1 or localhost).', file=sys.stderr)
        return 2
    server = make_server(args.host, args.port)
    runtime = {'pid': os.getpid(), 'port': server.server_address[1], 'host': args.host, 'python': sys.executable, 'command': sys.argv, 'started_at': time.time()}
    runtime_tmp = SERVER_RUNTIME_PATH.with_suffix('.tmp')
    runtime_tmp.write_text(json.dumps(runtime), encoding='utf-8')
    runtime_tmp.replace(SERVER_RUNTIME_PATH)
    scheduler = Scheduler()
    scheduler.start()
    print(f'PUBLIC_DEMO listening at http://{args.host}:{server.server_address[1]}')
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        SERVER_RUNTIME_PATH.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
