"""Local-only Douyin video pipeline. Requires separately installed model/tool binaries."""
import ctypes, json, os, pathlib, re, shutil, subprocess, sys, threading, time, urllib.request, wave
from concurrent.futures import ThreadPoolExecutor, as_completed

STAGES = ['download', 'audio', 'transcribe', 'translate', 'subtitles', 'tts', 'timing', 'render']
LABELS = {'download':'Tải / nhận video','audio':'Tách âm thanh','transcribe':'Nhận diện lời thoại','translate':'Dịch tiếng Việt','subtitles':'Tạo phụ đề','tts':'Tạo giọng đọc','timing':'Căn timing','render':'Ghép video'}

def stamp(seconds):
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'

def run(cmd, log, cancel=None, cwd=None):
    log('$ ' + ' '.join(str(x) for x in cmd))
    kwargs = {'cwd':cwd, 'stdout':subprocess.PIPE, 'stderr':subprocess.STDOUT, 'text':True, 'errors':'replace'}
    if os.name == 'nt': kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else: kwargs['start_new_session'] = True
    proc = subprocess.Popen(cmd, **kwargs)
    if cancel: cancel.proc = proc
    try:
        for line in proc.stdout: log(line.rstrip())
        code = proc.wait()
        if cancel and cancel.is_set(): raise InterruptedError('Đã dừng theo yêu cầu')
        if code: raise RuntimeError(f'{pathlib.Path(cmd[0]).name} kết thúc với mã {code}')
    finally:
        if cancel: cancel.proc = None

def probe(path, cfg):
    p = subprocess.run([cfg['ffprobe'], '-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)], capture_output=True,text=True,timeout=30)
    if p.returncode: raise RuntimeError('ffprobe không đọc được video: '+p.stderr[-500:])
    duration = float(p.stdout.strip());
    if duration < 30 or duration > 7200: raise RuntimeError('Chỉ nhận video từ 30 giây đến 2 giờ')
    return duration

def system_memory():
    """Read physical RAM without installing psutil or sampling pagefile usage."""
    if os.name == 'nt':
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_=[('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),('ullTotalPhys',ctypes.c_ulonglong),('ullAvailPhys',ctypes.c_ulonglong),('ullTotalPageFile',ctypes.c_ulonglong),('ullAvailPageFile',ctypes.c_ulonglong),('ullTotalVirtual',ctypes.c_ulonglong),('ullAvailVirtual',ctypes.c_ulonglong),('ullAvailExtendedVirtual',ctypes.c_ulonglong)]
        stats=MEMORYSTATUSEX();stats.dwLength=ctypes.sizeof(stats)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stats)):return None
        return {'total_mib':stats.ullTotalPhys//1048576,'free_mib':stats.ullAvailPhys//1048576,'load_percent':stats.dwMemoryLoad}
    try:
        values={}
        for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
            key,_,rest=line.partition(':')
            if key in ('MemTotal','MemAvailable'):values[key]=int(rest.split()[0])//1024
        total=values['MemTotal'];free=values['MemAvailable']
        return {'total_mib':total,'free_mib':free,'load_percent':round((total-free)/total*100)}
    except (OSError,KeyError,ValueError,ZeroDivisionError):return None

def require_memory(stage,cfg):
    memory=system_memory()
    floor=int(cfg.get('min_free_ram_mib',3000))
    if stage=='translate':floor=max(floor,3500)
    if memory and memory['free_mib']<floor:
        raise RuntimeError(f'RAM trống {memory["free_mib"]} MiB, dưới ngưỡng an toàn {floor} MiB cho bước {LABELS[stage]}. Đóng ứng dụng đang chiếm RAM rồi bấm Tiếp tục; không tự dùng pagefile để ép chạy.')

def preflight(cfg):
    def binary(name): return bool(shutil.which(cfg[name]) or pathlib.Path(cfg[name]).is_file())
    bins = {name:binary(name) for name in ('ffmpeg','ffprobe','yt_dlp','piper')}
    asr_ready=False;cpu_asr_ready=False
    try:
        import faster_whisper
        from faster_whisper.utils import download_model
        bins['faster_whisper'] = True
        def cached(model):
            if not model:return False
            if pathlib.Path(model).is_dir():return True
            try:return pathlib.Path(download_model(model,local_files_only=True)).is_dir()
            except Exception:return False
        asr_ready=cached(cfg.get('recognition_model'))
        cpu_asr_ready=cached(cfg.get('cpu_fallback_model') or cfg.get('recognition_model'))
    except Exception: bins['faster_whisper'] = False
    gpu = {'available':False,'free_mib':None,'name':None}
    try:
        out = subprocess.run(['nvidia-smi','--query-gpu=name,memory.free','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3)
        if out.returncode == 0:
            name, free = out.stdout.splitlines()[0].rsplit(',',1)
            gpu = {'available':True,'free_mib':int(free.strip()),'name':name.strip()}
    except (FileNotFoundError,ValueError,IndexError,subprocess.TimeoutExpired): pass
    voice = pathlib.Path(cfg.get('voice_path') or '')
    if cfg.get('tts_provider') == 'vieneu':
        onnx_dir=pathlib.Path(cfg.get('vieneu_onnx_dir') or '')
        codec_dir=pathlib.Path(cfg.get('vieneu_codec_dir') or '')
        required_onnx=('config.json','tokenizer.json','vieneu_prefill.onnx','vieneu_decode_step.onnx','vieneu_acoustic_cached.onnx','vieneu_backbone_shared.data','vieneu_v3_heads.npz')
        required_codec=('moss_audio_tokenizer_decode_full.onnx','moss_audio_tokenizer_decode_shared.data','moss_audio_tokenizer_decode_step.onnx')
        try:
            from importlib.util import find_spec
            package_ready=find_spec('vieneu') is not None
        except (ImportError,ValueError): package_ready=False
        voice_ok=package_ready and all((onnx_dir/name).is_file() for name in required_onnx) and all((codec_dir/name).is_file() for name in required_codec)
    elif not cfg.get('voice_path'): voice_ok=False
    else: voice_ok=voice.is_file() and voice.with_suffix('.onnx.json').is_file()
    ollama = False; model_ready=False; models=[]
    try:
        with urllib.request.urlopen(cfg['ollama_url'].rstrip('/')+'/api/tags', timeout=2) as resp:
            tags=json.load(resp);ollama = resp.status == 200
            models=[{'name':m.get('name') or m.get('model'),'size_mib':round((m.get('size') or 0)/1048576)} for m in tags.get('models',[]) if m.get('name') or m.get('model')]
            model_ready=any(m['name']==cfg.get('translation_model') for m in models)
    except Exception: pass
    return {'tools':bins,'recognition_model_ready':asr_ready,'cpu_fallback_model_ready':cpu_asr_ready,'voice_ready':bool(cfg.get('tts_command')) if cfg.get('tts_provider')=='custom' else voice_ok,'ollama_ready':ollama,'translation_model_ready':model_ready,'translation_models':models,'gpu':gpu,'memory':system_memory(),'checked_at':time.time()}

def read_json(path, fallback):
    try: return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError: return fallback

def write_json(path, data):
    p=pathlib.Path(path); p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp'); tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(p)

def transcribe_worker(audio_dir, segments_path, config_path, log_path):
    """Separate process so CUDA OOM can be retried on CPU without poisoning the worker."""
    from faster_whisper import WhisperModel
    cfg=read_json(config_path,{})
    device=cfg['device']; compute='int8_float16' if device == 'cuda' else 'int8'
    workers=1 if device=='cuda' else cfg.get('parallel_cpu',1)
    threads=min(cfg['cpu_threads'],max(1,8//workers)) if device=='cpu' else cfg['cpu_threads']
    model=WhisperModel(cfg['model'], device=device, compute_type=compute, cpu_threads=threads, num_workers=workers, local_files_only=True)
    files=sorted(pathlib.Path(audio_dir).glob('chunk_*.wav'))
    offsets=[]; offset=0
    for path in files:
        with wave.open(str(path),'rb') as f: duration=f.getnframes()/f.getframerate()
        offsets.append(offset)
        offset+=duration
    def one(i):
        chunks, _ = model.transcribe(str(files[i]), language=None if cfg.get('language') in ('auto','') else cfg.get('language'), vad_filter=True, word_timestamps=True, beam_size=2)
        offset=offsets[i]
        return i,[{'start':round(offset+seg.start,3),'end':round(offset+seg.end,3),'source':seg.text.strip(),'target':'','words':[{'start':round(offset+w.start,3),'end':round(offset+w.end,3),'text':w.word} for w in (seg.words or [])]} for seg in chunks if seg.text.strip()]
    done={}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(one,i) for i in range(len(files))]):
            i,rows=future.result();done[i]=rows
            with open(log_path,'a',encoding='utf-8') as f: f.write(f'Đã nhận diện đoạn {len(done)}/{len(files)}\n')
            write_json(segments_path,[row for k in sorted(done) for row in done[k]])

class PiperSession:
    def __init__(self, cfg, log, cancel, folder):
        self.log=log; self.cancel=cancel
        help_result=subprocess.run([cfg['piper'],'--help'],capture_output=True,text=True,timeout=6)
        if '--json-input' in (help_result.stdout+help_result.stderr):
            command=[cfg['piper'],'--model',cfg['voice_path'],'--json-input']
        else:
            command=[sys.executable,str(pathlib.Path(__file__).resolve().parent/'piper_worker.py'),cfg['voice_path']]
            log('Piper CLI không có --json-input; dùng PiperVoice trong Python để giữ model persistent')
        self.error_file=folder/'piper-error.log'
        self.error_stream=self.error_file.open('w',encoding='utf-8')
        self.proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=self.error_stream,text=True,bufsize=1)
        cancel.proc=self.proc
    def speak(self, text, path):
        self.cancel_check()
        self.proc.stdin.write(json.dumps({'text':text,'output_file':str(path)},ensure_ascii=False)+'\n'); self.proc.stdin.flush()
        stable=0;last=-1
        for _ in range(1200):
            self.cancel_check()
            if path.exists() and path.stat().st_size > 44:
                try:
                    size=path.stat().st_size
                    with wave.open(str(path),'rb') as f: expected=f.getnframes()*f.getsampwidth()*f.getnchannels()+44
                    stable=stable+1 if size==last and size>=expected else 0;last=size
                    if stable>=5:return
                except (EOFError,wave.Error): pass
            if self.proc.poll() is not None:
                self.error_stream.flush()
                raise RuntimeError('Piper dừng bất thường: '+self.error_file.read_text(encoding='utf-8')[-1200:])
            time.sleep(.1)
        raise TimeoutError('Piper không xuất WAV trong 120 giây')
    def cancel_check(self):
        if self.cancel.is_set(): raise InterruptedError('Đã dừng theo yêu cầu')
    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired: self.proc.kill()
        self.error_stream.close()
        self.cancel.proc=None

def translate_batch(rows, cfg, log, cancel, segments_path):
    for start in range(0,len(rows),8):
        if cancel.is_set(): raise InterruptedError('Đã dừng theo yêu cầu')
        batch=rows[start:start+8]
        if all(r.get('target') for r in batch): continue
        payload={'model':cfg['translation_model'],'stream':False,'format':'json','prompt': 'Dịch lời thoại sau sang tiếng Việt tự nhiên, giữ nguyên ý và tên riêng. Chỉ trả JSON dạng {"items":[{"id":0,"text":"..."}]}. Không bỏ câu, không thêm lời giải thích.\n'+json.dumps([{'id':i,'text':r['source']} for i,r in enumerate(batch)],ensure_ascii=False)}
        req=urllib.request.Request(cfg['ollama_url'].rstrip('/')+'/api/generate',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=180) as response: data=json.load(response)
            obj=json.loads(data['response']); found={int(x['id']):str(x['text']).strip() for x in obj['items']}
            if len(found)!=len(batch) or any(i not in found or not found[i] for i in range(len(batch))): raise ValueError('provider_incomplete: thiếu câu dịch')
            for i,row in enumerate(batch): row['target']=found[i]
            write_json(segments_path,rows)
        except Exception as exc:
            log('provider_incomplete / câu bị chặn: '+str(exc))
            raise RuntimeError('Provider dịch trả thiếu câu hoặc từ chối. Sửa bản dịch trong bảng phụ đề rồi chạy lại từ bước Tạo phụ đề. Chi tiết: '+str(exc)) from exc
        log(f'Đã dịch {min(start+8,len(rows))}/{len(rows)} câu')

def process_stage(stage, job, cfg, root, log, cancel):
    folder=root/'jobs'/job['id']; folder.mkdir(parents=True,exist_ok=True)
    source=folder/'source.mp4'; audio=folder/'audio'; segments=folder/'segments.json'; output=folder/'output.mp4'
    if stage in ('transcribe','translate','tts','timing','render'):require_memory(stage,cfg)
    if stage=='download':
        if job.get('upload_path'):
            source=pathlib.Path(job['upload_path']); job['source_path']=str(source)
        else:
            template=str(folder/'source.%(ext)s')
            command=[cfg['yt_dlp'],'--no-playlist','--merge-output-format','mp4','-o',template]
            if cfg.get('cookies_path'):
                if not pathlib.Path(cfg['cookies_path']).is_file(): raise RuntimeError('Không tìm thấy file cookies đã cấu hình')
                command+=['--cookies',cfg['cookies_path']]
            run(command+[job['url']],log,cancel)
            candidates=list(folder.glob('source.*')); source=next((p for p in candidates if p.suffix=='.mp4'), candidates[0] if candidates else None)
            if source is None: raise RuntimeError('Không tìm thấy video sau tải')
            job['source_path']=str(source)
        job['duration']=probe(source,cfg)
    elif stage=='audio':
        audio.mkdir(exist_ok=True)
        for old in audio.glob('chunk_*.wav'): old.unlink()
        run([cfg['ffmpeg'],'-hide_banner','-loglevel','error','-y','-i',job['source_path'],'-vn','-ac','1','-ar','16000','-f','segment','-segment_time',str(cfg['chunk_seconds']),str(audio/'chunk_%04d.wav')],log,cancel)
        if not list(audio.glob('chunk_*.wav')): raise RuntimeError('Không tạo được đoạn âm thanh')
        job['chunks']=len(list(audio.glob('chunk_*.wav')))
    elif stage=='transcribe':
        device='cpu'
        if cfg['gpu_policy']!='cpu':
            health=preflight(cfg)['gpu']
            if health['available'] and health['free_mib'] >= int(cfg['vram_floor_mib']): device='cuda'
            elif cfg['gpu_policy']=='require_gpu': raise RuntimeError('GPU không đủ VRAM trống hoặc không khả dụng')
            else: log('GPU không đạt ngưỡng VRAM; chuyển CPU')
        params={'device':device,'model':cfg['recognition_model'],'cpu_threads':cfg['cpu_threads'],'parallel_cpu':cfg['parallel_cpu'],'language':cfg['source_language']}
        params_path=folder/'asr_config.json'; write_json(params_path,params)
        command=[sys.executable,str(pathlib.Path(__file__).resolve()),'--transcribe',str(audio),str(segments),str(params_path),str(folder/'asr.log')]
        try: run(command,log,cancel)
        except RuntimeError:
            if device=='cuda' and cfg['gpu_policy']=='auto' and not cancel.is_set():
                log('CUDA lỗi; thử lại nhận diện trên CPU')
                params['device']='cpu';params['model']=cfg.get('cpu_fallback_model') or cfg['recognition_model']; write_json(params_path,params); run(command,log,cancel)
            else: raise
        rows=read_json(segments,[])
        if not rows: raise RuntimeError('Whisper không nhận diện được lời thoại; kiểm tra âm thanh')
        job['sentences']=len(rows)
    elif stage=='translate':
        if not preflight(cfg)['translation_model_ready']:raise RuntimeError('Model dịch đã chọn chưa có trong Ollama local; chọn model đã có ở Cấu hình, rồi chạy lại từ bước Dịch')
        rows=read_json(segments,[]); translate_batch(rows,cfg,log,cancel,segments)
    elif stage=='subtitles':
        rows=read_json(segments,[])
        if not rows or any(not r.get('target') or r['end']<=r['start'] for r in rows): raise RuntimeError('Thiếu câu dịch hoặc timing; sửa trong bảng phụ đề')
        (folder/'subtitles.srt').write_text('\n\n'.join(f"{i+1}\n{stamp(r['start'])} --> {stamp(r['end'])}\n{r['target']}" for i,r in enumerate(rows))+'\n',encoding='utf-8')
        (folder/'subtitles.vtt').write_text('WEBVTT\n\n'+'\n\n'.join(f"{stamp(r['start']).replace(',', '.')} --> {stamp(r['end']).replace(',', '.')}\n{r['target']}" for r in rows)+'\n',encoding='utf-8')
    elif stage=='tts':
        rows=read_json(segments,[]); voice_dir=folder/'voice'; voice_dir.mkdir(exist_ok=True)
        if cfg.get('tts_provider')=='vieneu':
            from vieneu import Vieneu
            tts=Vieneu(backend='onnx',onnx_dir=cfg.get('vieneu_onnx_dir') or None,codec_dir=cfg.get('vieneu_codec_dir') or None,onnx_subfolder='onnx_update',threads=max(1,min(int(cfg.get('cpu_threads',4)),4)))
            voice=cfg.get('voice_path') or None
            for i,row in enumerate(rows):
                path=voice_dir/f'{i:05}.wav'
                if not path.exists() or path.stat().st_size<=44:
                    tts.save(tts.infer(row['target'],voice=voice),path)
                if i%10==0 or i==len(rows)-1:log(f'Giọng đọc {i+1}/{len(rows)}')
        elif cfg.get('tts_provider')=='custom':
            template=cfg.get('tts_command')
            if not isinstance(template,list) or not template: raise RuntimeError('Cần khai báo lệnh TTS local dạng mảng JSON')
            for i,row in enumerate(rows):
                path=voice_dir/f'{i:05}.wav'
                if not path.exists() or path.stat().st_size<=44:
                    command=[str(x).replace('{text}',row['target']).replace('{output}',str(path)).replace('{model}',cfg.get('voice_path','')) for x in template]
                    run(command,log,cancel)
                    if not path.exists(): raise RuntimeError('Adapter TTS không tạo file WAV: '+str(path))
                if i%10==0 or i==len(rows)-1:log(f'Giọng đọc {i+1}/{len(rows)}')
        else:
            if not cfg.get('voice_path') or not pathlib.Path(cfg['voice_path']).is_file(): raise RuntimeError('Chưa chọn model giọng Piper .onnx có trên máy')
            session=PiperSession(cfg,log,cancel,folder)
            try:
                for i,row in enumerate(rows):
                    path=voice_dir/f'{i:05}.wav'
                    if not path.exists() or path.stat().st_size<=44: session.speak(row['target'],path)
                    if i%10==0 or i==len(rows)-1: log(f'Giọng đọc {i+1}/{len(rows)}')
            finally: session.close()
    elif stage=='timing':
        rows=read_json(segments,[]); out_dir=folder/'timed'; out_dir.mkdir(exist_ok=True)
        notes=[]
        for i,row in enumerate(rows):
            src=folder/'voice'/f'{i:05}.wav'; dst=out_dir/f'{i:05}.wav'
            with wave.open(str(src),'rb') as f: voice_len=f.getnframes()/f.getframerate()
            budget=max(.15,row['end']-row['start']); speed=voice_len/budget
            if speed>1.5: notes.append(f'Câu {i+1}: dài {voice_len:.1f}s / cửa sổ {budget:.1f}s; cần xem lại timing')
            if speed>1.02:
                # atempo accepts 0.5..2.0; chain when source speech greatly exceeds slot.
                factors=[]; remaining=speed
                while remaining>2: factors.append(2.0); remaining/=2
                factors.append(remaining)
                run([cfg['ffmpeg'],'-hide_banner','-loglevel','error','-y','-i',str(src),'-filter:a',','.join('atempo=%.4f'%x for x in factors),str(dst)],log,cancel)
            else: shutil.copyfile(src,dst)
        write_json(folder/'timing_notes.json',notes)
        if notes: log(f'{len(notes)} câu cần nghe và kiểm tra lại timing')
    elif stage=='render':
        rows=read_json(segments,[]); voice=folder/'narration.wav'
        # Stream into a WAV, rather than loading a 2-hour recording into memory.
        sample_rate=22050
        with wave.open(str(voice),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(sample_rate)
            pos=0
            for i,row in enumerate(rows):
                target=max(0,round(row['start']*sample_rate))
                if target>pos:
                    remain=target-pos
                    while remain:
                        n=min(remain,65536);out.writeframesraw(b'\0\0'*n);remain-=n
                    pos=target
                # Normalize each Piper voice to the common narration rate/format.
                wav=folder/'timed'/f'{i:05}.wav'; pcm=folder/'timed'/f'{i:05}.pcm'
                run([cfg['ffmpeg'],'-hide_banner','-loglevel','error','-y','-i',str(wav),'-f','s16le','-ac','1','-ar',str(sample_rate),str(pcm)],log,cancel)
                with pcm.open('rb') as f:
                    while chunk:=f.read(131072): out.writeframesraw(chunk);pos+=len(chunk)//2
                pcm.unlink()
        source=job['source_path']
        run([cfg['ffmpeg'],'-hide_banner','-loglevel','error','-y','-i',source,'-i',str(voice),'-i',str(folder/'subtitles.srt'),'-filter_complex','[0:a]volume=0.22[bg];[bg][1:a]amix=inputs=2:duration=first:normalize=0[a]', '-map','0:v:0','-map','[a]','-map','2:0','-c:v','libx264','-preset',cfg['encode_preset'],'-crf',str(cfg['crf']),'-c:a','aac','-b:a','160k','-c:s','mov_text','-movflags','+faststart','-t',str(job['duration']),str(output)],log,cancel)
        job['output_path']=str(output)
    return job

if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='--transcribe':
    transcribe_worker(*sys.argv[2:6])
