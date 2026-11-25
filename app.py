"""Local HTTP control panel. Start with `python app.py`; binds only to loopback."""
import json, mimetypes, os, pathlib, re, shutil, sqlite3, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from engine import STAGES, LABELS, preflight, process_stage, read_json, write_json, system_memory

BASE=pathlib.Path(__file__).resolve().parent
if sys.stdout is None:
    try:
        sys.stdout=(BASE/'engine-service.log').open('a',encoding='utf-8')
        sys.stderr=sys.stdout
    except OSError:
        pass
DATA=BASE/'data'; DATA.mkdir(exist_ok=True)
REMOTE_CONFIG=BASE/'remote-access.json'
DB=DATA/'studio.sqlite3'; DIST=BASE/'dist'; LOCK=threading.RLock(); WAKE=threading.Event()
SITE_ORIGIN='https://douyin-studio-van-2026.hushedopal1.chatgpt.site'
def remote_access_config():
    if not REMOTE_CONFIG.is_file(): return '', '', ''
    cfg=json.loads(REMOTE_CONFIG.read_text(encoding='utf-8'))
    origin=str(cfg.get('origin','')).strip().rstrip('/')
    email=str(cfg.get('access_email','')).strip()
    if not origin: return '', '', ''
    parsed=urlsplit(origin); host=(parsed.hostname or '').lower()
    hostname_pattern=r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if parsed.scheme!='https' or not re.fullmatch(hostname_pattern,host) or parsed.netloc.lower()!=host or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('remote-access.json phải chứa đúng origin HTTPS theo hostname đã cấu hình, không có path/port/query')
    if not email or '@' not in email: raise ValueError('remote-access.json phải giới hạn đúng email Cloudflare Access')
    return origin, host, email
REMOTE_ORIGIN, REMOTE_HOST, ACCESS_EMAIL = remote_access_config()
ALLOWED_CORS_ORIGINS={SITE_ORIGIN}
if REMOTE_ORIGIN: ALLOWED_CORS_ORIGINS.add(REMOTE_ORIGIN)
DEFAULT={'profile_mode':'auto','source_language':'zh','target_language':'vi','recognition_model':'small','cpu_fallback_model':'base','translation_model':'qwen2.5:1.5b','voice_path':'','tts_provider':'piper','tts_command':[],'gpu_policy':'auto','vram_floor_mib':2600,'min_free_ram_mib':3000,'cpu_threads':3,'parallel_cpu':1,'chunk_seconds':120,'encode_preset':'superfast','crf':23,'ffmpeg':'ffmpeg','ffprobe':'ffprobe','yt_dlp':'yt-dlp','cookies_path':'','piper':'piper','ollama_url':'http://127.0.0.1:11434'}
def connect():
    db=sqlite3.connect(DB); db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL'); return db
with connect() as db:
    db.executescript('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL, position INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, data TEXT NOT NULL, started REAL NOT NULL); CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);')
    db.execute('UPDATE jobs SET data=json_set(data, "$.status", "stopped") WHERE json_extract(data, "$.status")="running"')
def profile_defaults():
    memory=system_memory()
    if memory and memory['total_mib']>=28*1024:
        return {'cpu_threads':4,'parallel_cpu':2,'chunk_seconds':300,'min_free_ram_mib':4500,'vram_floor_mib':2600}
    return {'cpu_threads':3,'parallel_cpu':1,'chunk_seconds':120,'min_free_ram_mib':3000,'vram_floor_mib':2600}
def settings():
    with connect() as db: row=db.execute('SELECT value FROM settings WHERE key="config"').fetchone()
    cfg={**DEFAULT,**(json.loads(row['value']) if row else {})}
    if cfg['profile_mode']=='auto':cfg.update(profile_defaults())
    return cfg
def all_jobs():
    with connect() as db: return [json.loads(r['data']) for r in db.execute('SELECT data FROM jobs ORDER BY position,created')]
def get_job(jid):
    with connect() as db: row=db.execute('SELECT data FROM jobs WHERE id=?',(jid,)).fetchone()
    if not row: raise KeyError('Không tìm thấy video')
    return json.loads(row['data'])
def save_job(job):
    with LOCK, connect() as db: db.execute('UPDATE jobs SET data=? WHERE id=?',(json.dumps(job,ensure_ascii=False),job['id']))
def log(job, message):
    path=DATA/'jobs'/job['id']/'process.log'; path.parent.mkdir(parents=True,exist_ok=True)
    text=re.sub(r'(https?://[^\s]+)', '[URL]',str(message))[:1400]
    with path.open('a',encoding='utf-8') as f: f.write(time.strftime('%H:%M:%S')+'  '+text+'\n')
class Cancel(threading.Event):
    proc=None
    def stop(self):
        self.set(); p=self.proc
        if p and p.poll() is None:
            p.terminate()
            try: p.wait(timeout=2)
            except subprocess.TimeoutExpired: p.kill()
ACTIVE={}
def scheduler():
    while True:
        WAKE.wait(1); WAKE.clear()
        with LOCK:
            if ACTIVE: continue
            pending=next((j for j in all_jobs() if j['status']=='queued'),None)
            if not pending: continue
            cancel=Cancel(); ACTIVE[pending['id']]=cancel
        try: execute(pending['id'],cancel)
        finally:
            with LOCK: ACTIVE.pop(pending['id'],None)
            WAKE.set()
def execute(jid,cancel):
    job=get_job(jid); cfg=job.get('config') or settings(); stage_names=STAGES
    if cfg.get('profile_mode','auto')=='auto':cfg={**cfg,**profile_defaults()}
    run_id=uuid.uuid4().hex[:12]; started=time.time(); job['config']=cfg;job['status']='running'; job['started_at']=started;job['run_id']=run_id;save_job(job)
    history={'id':run_id,'job_id':jid,'started_at':started,'config':cfg,'stages':{},'status':'running','duration':job.get('duration')}
    with connect() as db: db.execute('INSERT INTO runs VALUES(?,?,?,?)',(run_id,jid,json.dumps(history,ensure_ascii=False),started))
    try:
        for stage in stage_names:
            job=get_job(jid)
            if job['stages'][stage]=='done': continue
            if cancel.is_set(): raise InterruptedError('Đã dừng')
            job['current_stage']=stage;job['stages'][stage]='running';job['stage_started_at']=time.time();save_job(job)
            log(job,'Bắt đầu: '+LABELS[stage]); tick=time.monotonic()
            job=process_stage(stage,job,cfg,DATA,lambda msg:log(job,msg),cancel)
            elapsed=round(time.monotonic()-tick,2)
            job['stages'][stage]='done';job['stage_times'][stage]=elapsed;save_job(job)
            history['stages'][stage]=elapsed;history['duration']=job.get('duration');log(job,f'Xong {LABELS[stage]} · {elapsed:.1f}s')
        job['status']='completed';job['current_stage']=None;history['status']='completed'
        if job.get('output_path'):
            original=pathlib.Path(job['output_path']);versioned=original.with_name('output_'+run_id+'.mp4')
            original.replace(versioned);job['output_path']=str(versioned);history['output_path']=str(versioned)
        subs=DATA/'jobs'/jid/'subtitles.srt'
        if subs.exists():
            versioned_subs=subs.with_name('subtitles_'+run_id+'.srt');shutil.copyfile(subs,versioned_subs);history['subtitles_path']=str(versioned_subs)
    except InterruptedError:
        job=get_job(jid);job['status']='stopped';job['stages'][job['current_stage']]='stopped';history['status']='stopped';log(job,'Đã dừng; giữ file trung gian')
    except Exception as exc:
        job=get_job(jid);job['status']='blocked';job['error']=str(exc)[:1200];job['stages'][job['current_stage']]='blocked';history['status']='blocked';history['error']=job['error'];log(job,'LỖI: '+str(exc))
    finally:
        job['finished_at']=time.time();history['finished_at']=job['finished_at'];save_job(job)
        with connect() as db: db.execute('UPDATE runs SET data=? WHERE id=?',(json.dumps(history,ensure_ascii=False),run_id))
threading.Thread(target=scheduler,daemon=True).start()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def authorize_remote(self):
        host=(urlsplit('//'+self.headers.get('Host','')).hostname or '').lower()
        if REMOTE_HOST and host==REMOTE_HOST and self.headers.get('Cf-Access-Authenticated-User-Email','').casefold()!=ACCESS_EMAIL.casefold():
            self.fail('Email Cloudflare Access này không được phép dùng engine',403)
            return False
        return True
    def end_headers(self):
        origin=self.headers.get('Origin','')
        if origin in ALLOWED_CORS_ORIGINS:
            self.send_header('Access-Control-Allow-Origin',origin)
            self.send_header('Access-Control-Allow-Private-Network','true')
            self.send_header('Vary','Origin')
        super().end_headers()
    def do_OPTIONS(self):
        if not self.authorize_remote(): return
        private_network=self.headers.get('Access-Control-Request-Private-Network','').lower()
        if self.headers.get('Origin') not in ALLOWED_CORS_ORIGINS or private_network not in ('','true'):
            self.send_response(403);self.end_headers();return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type, X-Local-Action, X-Offset')
        if private_network=='true': self.send_header('Access-Control-Allow-Private-Network','true')
        self.end_headers()
    def json(self,value,status=200):
        payload=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
    def fail(self,e,status=400): self.json({'error':str(e)},status)
    def body(self,max_size=1000000):
        n=int(self.headers.get('Content-Length') or '0')
        if n<0 or n>max_size: raise ValueError('Dữ liệu yêu cầu quá lớn')
        return json.loads(self.rfile.read(n)) if n else {}
    def do_GET(self):
        if not self.authorize_remote(): return
        path=urlsplit(self.path).path
        try:
            if path=='/api/state':
                with connect() as db: histories=[json.loads(r['data']) for r in db.execute('SELECT data FROM runs ORDER BY started DESC LIMIT 60')]
                return self.json({'jobs':all_jobs(),'runs':histories,'config':settings(),'checklist':read_json(DATA/'checklist.json',[]),'health':preflight(settings()),'stages':LABELS})
            m=re.fullmatch(r'/api/jobs/([a-f0-9]+)/segments',path)
            if m: return self.json(read_json(DATA/'jobs'/m[1]/'segments.json',[]))
            m=re.fullmatch(r'/api/jobs/([a-f0-9]+)/log',path)
            if m:
                get_job(m[1]); f=DATA/'jobs'/m[1]/'process.log';return self.json(f.read_text(encoding='utf-8')[-24000:] if f.exists() else '')
            m=re.fullmatch(r'/api/runs/([a-f0-9]+)/(output|subtitles)',path)
            if m:
                with connect() as db: row=db.execute('SELECT data FROM runs WHERE id=?',(m[1],)).fetchone()
                if not row:return self.fail('Phiên không tồn tại',404)
                run_info=json.loads(row['data']);file=pathlib.Path(run_info.get(m[2]+'_path') or '')
                if not file.is_file() or not file.resolve().is_relative_to((DATA/'jobs').resolve()):return self.fail('File phiên không còn',404)
                return self.serve_file(file)
            m=re.fullmatch(r'/api/jobs/([a-f0-9]+)/(source|output|subtitles|vtt|narration)',path)
            if m:
                job=get_job(m[1]); name=m[2]
                file=pathlib.Path(job['source_path']) if name=='source' and job.get('source_path') else pathlib.Path(job['output_path']) if name=='output' and job.get('output_path') else DATA/'jobs'/m[1]/({'output':'output.mp4','subtitles':'subtitles.srt','vtt':'subtitles.vtt','narration':'narration.wav','source':'source.mp4'}[name])
                if not file.is_file(): return self.fail('File chưa có',404)
                return self.serve_file(file)
            file=DIST/('index.html' if path=='/' else path.lstrip('/'))
            if not file.resolve().is_relative_to(DIST.resolve()) or not file.is_file():return self.fail('Không tìm thấy',404)
            data=file.read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        except Exception as e: self.fail(e,500)
    def serve_file(self,file):
        size=file.stat().st_size;start=0;end=size-1;header=self.headers.get('Range','')
        if header:
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',header)
            if match: start=int(match[1]);end=min(size-1,int(match[2])) if match[2] else end
            if start>=size or end<start: self.send_response(416);self.end_headers();return
        length=end-start+1;self.send_response(206 if header else 200);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(length));self.send_header('Accept-Ranges','bytes');self.send_header('Content-Disposition',('attachment' if self.headers.get('X-Download')=='1' else 'inline')+f'; filename="{file.name}"')
        if header:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with file.open('rb') as f:
            f.seek(start)
            while length:
                b=f.read(min(length,1024*1024));self.wfile.write(b);length-=len(b)
    def do_POST(self):
        if not self.authorize_remote(): return
        path=urlsplit(self.path).path
        try:
            origin=self.headers.get('Origin','')
            allowed_post_origins=ALLOWED_CORS_ORIGINS|{'http://127.0.0.1:8765','http://localhost:8765'}
            if origin and origin not in allowed_post_origins: return self.fail('Origin không hợp lệ',403)
            if self.headers.get('X-Local-Action')!='1': return self.fail('Thiếu xác nhận local action',403)
            if path=='/api/upload/start':
                body=self.body(); filename=pathlib.Path(body.get('name','video.mp4')).name
                if pathlib.Path(filename).suffix.lower() not in ('.mp4','.mov','.mkv','.webm'): raise ValueError('File phải là video MP4, MOV, MKV hoặc WEBM')
                token=uuid.uuid4().hex; folder=DATA/'uploads';folder.mkdir(exist_ok=True);(folder/token).touch();write_json(folder/(token+'.json'),{'name':filename,'size':int(body['size']),'received':0});return self.json({'token':token})
            m=re.fullmatch(r'/api/upload/([a-f0-9]{32})',path)
            if m:
                token=m[1]; info_path=DATA/'uploads'/(token+'.json');info=read_json(info_path,None)
                if not info: raise ValueError('Phiên upload không tồn tại')
                n=int(self.headers.get('Content-Length') or '0');offset=int(self.headers.get('X-Offset') or '-1')
                if n<1 or n>8*1024*1024 or offset!=info['received'] or offset+n>info['size']:raise ValueError('Chunk upload không hợp lệ')
                with (DATA/'uploads'/token).open('ab') as f:
                    remain=n
                    while remain:
                        chunk=self.rfile.read(min(remain,1024*1024))
                        if not chunk: raise ValueError('Upload bị ngắt')
                        f.write(chunk);remain-=len(chunk)
                info['received']+=n;write_json(info_path,info);return self.json({'received':info['received']})
            if path=='/api/jobs':
                body=self.body();url=str(body.get('url','')).strip();token=str(body.get('upload','')).strip()
                if bool(url)==bool(token):raise ValueError('Cần đúng một link hoặc một file')
                uploaded=None
                if url:
                    parsed=urlsplit(url)
                    if parsed.scheme not in ('https','http') or not parsed.netloc:raise ValueError('Link video không hợp lệ')
                else:
                    if not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('Mã upload không hợp lệ')
                    info=read_json(DATA/'uploads'/(token+'.json'),None)
                    if not info or info['received']!=info['size']:raise ValueError('Video chưa upload xong')
                    uploaded=str(DATA/'uploads'/token)
                cfg=settings();jid=uuid.uuid4().hex[:12];jobs=all_jobs()
                job={'id':jid,'title':str(body.get('title') or (pathlib.Path(info['name']).stem if uploaded else urlsplit(url).netloc))[:100], 'url':url,'upload_path':uploaded,'created_at':time.time(),'status':'queued','current_stage':None,'stages':{s:'waiting' for s in STAGES},'stage_times':{},'config':cfg,'notes':'','error':None,'duration':None,'sentences':0,'chunks':0}
                with connect() as db: db.execute('INSERT INTO jobs VALUES(?,?,?,?)',(jid,json.dumps(job,ensure_ascii=False),job['created_at'],len(jobs)))
                WAKE.set();return self.json(job,201)
            if path=='/api/config':
                body=self.body();cfg={**settings(),**{k:v for k,v in body.items() if k in DEFAULT}}
                if cfg['profile_mode'] not in ('auto','manual') or cfg['gpu_policy'] not in ('auto','cpu','require_gpu') or cfg['tts_provider'] not in ('piper','custom') or cfg['encode_preset'] not in ('ultrafast','superfast','veryfast','faster','fast','medium'):raise ValueError('Giá trị cấu hình không hợp lệ')
                from urllib.parse import urlsplit as parse_url
                if parse_url(cfg['ollama_url']).hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('Ollama phải chạy cục bộ trên máy này')
                if isinstance(cfg['tts_command'],str):
                    cfg['tts_command']=json.loads(cfg['tts_command'] or '[]')
                if not isinstance(cfg['tts_command'],list) or len(cfg['tts_command'])>20 or any(not isinstance(x,str) or len(x)>1000 for x in cfg['tts_command']):raise ValueError('Lệnh TTS phải là mảng JSON các đối số')
                for k,lo,hi in [('chunk_seconds',30,600),('parallel_cpu',1,4),('cpu_threads',1,8),('crf',16,32),('vram_floor_mib',512,4096),('min_free_ram_mib',512,16384)]:
                    cfg[k]=int(cfg[k]);
                    if not lo<=cfg[k]<=hi:raise ValueError(k+' ngoài giới hạn')
                if cfg['profile_mode']=='auto':cfg.update(profile_defaults())
                with connect() as db:db.execute('INSERT OR REPLACE INTO settings VALUES("config",?)',(json.dumps(cfg,ensure_ascii=False),))
                return self.json({'config':cfg,'health':preflight(cfg)})
            if path=='/api/checklist':
                body=self.body();items=body.get('items',[])
                if not isinstance(items,list) or len(items)>100 or any(not isinstance(x,dict) or len(str(x.get('text','')))>300 for x in items):raise ValueError('Checklist không hợp lệ')
                write_json(DATA/'checklist.json',items);return self.json(items)
            m=re.fullmatch(r'/api/jobs/([a-f0-9]+)/(stop|resume|rerun|notes|segments|move)',path)
            if m:
                jid,action=m.groups();body=self.body();job=get_job(jid)
                with LOCK:
                    if action=='stop':
                        if jid in ACTIVE: ACTIVE[jid].stop()
                        elif job['status']=='queued':job['status']='stopped';save_job(job)
                    elif action in ('resume','rerun'):
                        if jid in ACTIVE or job['status']=='running':raise ValueError('Video đang xử lý')
                        if action=='rerun':
                            stage=body.get('stage');
                            if stage not in STAGES:raise ValueError('Bước không hợp lệ')
                            for s in STAGES[STAGES.index(stage):]:job['stages'][s]='waiting';job['stage_times'].pop(s,None)
                            folder=DATA/'jobs'/jid
                            if STAGES.index(stage)<=STAGES.index('translate'):
                                segments=folder/'segments.json';rows=read_json(segments,[])
                                for row in rows:row['target']=''
                                if rows:write_json(segments,rows)
                            if STAGES.index(stage)<=STAGES.index('tts'):
                                for wav in (folder/'voice').glob('*.wav'):wav.unlink()
                            job['output_path']=None
                            job['config']=settings();job['error']=None
                        job['status']='queued';job['current_stage']=None;save_job(job);WAKE.set()
                    elif action=='notes':job['notes']=str(body.get('notes',''))[:5000];save_job(job)
                    elif action=='segments':
                        if jid in ACTIVE:raise ValueError('Dừng job trước khi sửa phụ đề')
                        path=DATA/'jobs'/jid/'segments.json';rows=read_json(path,[]);edits=body.get('rows',[])
                        if len(edits)!=len(rows):raise ValueError('Số câu không khớp')
                        for row,change in zip(rows,edits):
                            a,b=float(change['start']),float(change['end'])
                            if a<0 or b<=a or b>float(job.get('duration') or 7200)+1:raise ValueError('Timing không hợp lệ')
                            row.update(start=a,end=b,target=str(change['target']).strip()[:1200])
                        write_json(path,rows)
                        for s in STAGES[STAGES.index('subtitles'):]:job['stages'][s]='waiting'
                        job['output_path']=None
                        job['status']='stopped';save_job(job)
                    elif action=='move':
                        delta=int(body.get('delta',0));jobs=all_jobs();ix=next(i for i,j in enumerate(jobs) if j['id']==jid);other=max(0,min(len(jobs)-1,ix+delta));jobs.insert(other,jobs.pop(ix))
                        with connect() as db:
                            for i,j in enumerate(jobs):db.execute('UPDATE jobs SET position=? WHERE id=?',(i,j['id']))
                return self.json(job)
            return self.fail('Không tìm thấy',404)
        except Exception as e:self.fail(e)

if __name__=='__main__':
    host='127.0.0.1';port=int(os.environ.get('DOUYIN_STUDIO_PORT','8765'))
    print(f'Douyin Studio: http://{host}:{port}',flush=True)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
