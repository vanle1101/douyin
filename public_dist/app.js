const $ = (selector) => document.querySelector(selector);
const els = {
  status: $('#engine-status'), dot: $('#status-dot'), resources: $('#resource-summary'), notice: $('#notice'),
  file: $('#video-file'), fileTitle: $('#file-title'), fileSubtitle: $('#file-subtitle'), create: $('#create-button'),
  dropzone: $('#dropzone'), uploadProgress: $('#upload-progress'), uploadLabel: $('#upload-label'), uploadPercent: $('#upload-percent'), uploadBar: $('#upload-bar'),
  empty: $('#empty-job'), content: $('#job-content'), heading: $('#job-heading'), statusChip: $('#job-status'), duration: $('#job-duration'),
  stageCopy: $('#stage-copy'), jobPercent: $('#job-percent'), jobBar: $('#job-bar'), stages: $('#stage-list'), error: $('#job-error'),
  actions: $('#output-actions'), downloadMp4: $('#download-mp4'), downloadSrt: $('#download-srt'), mp4DeliveryNote: $('#mp4-delivery-note'), preview: $('#preview-panel'), video: $('#video-wrap'), videoDeliveryNote: $('#video-delivery-note'),
  transcript: $('#transcript-list'), logPanel: $('#log-panel'), events: $('#event-list'), cancel: $('#cancel-job'), deleteJob: $('#delete-job'),
  limitDuration: $('#limit-duration'), limitUpload: $('#limit-upload'), limitRuntime: $('#limit-runtime'), limitDisk: $('#limit-disk'), limitOutput: $('#limit-output')
};
const stageNames = ['Tải video','Tách âm thanh','Nhận diện lời thoại','Dịch phụ đề','Tạo phụ đề','Tạo giọng đọc','Căn timing','Ghép video'];
const limits = { maxBytes: 8 * 1024 * 1024 * 1024, chunkBytes: 16 * 1024 * 1024, maxSeconds: 7200 };
let selectedFile = null;
let currentJob = null;
let lastSegments = '';
let lastEvents = '';
let busy = false;
let mp4DeliveryRestricted = false;
let pipelineReady = false;

function prettyBytes(value) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
function prettyDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours} giờ${minutes ? ` ${minutes} phút` : ''}` : `${minutes} phút`;
}
function showNotice(message, kind = 'warning') {
  els.notice.textContent = message;
  els.notice.className = `notice ${kind}`;
  els.notice.hidden = !message;
}
function validSelectedFile() {
  if (!selectedFile) return false;
  const ext = selectedFile.name.toLowerCase().split('.').pop();
  return ['mp4','mov','mkv','webm'].includes(ext) && selectedFile.size >= 64 * 1024 && selectedFile.size <= limits.maxBytes && selectedFile.size <= 8 * 1024 * 1024 * 1024;
}
function syncCreateButton() {
  if (!els.create) return;
  els.create.disabled = !pipelineReady || busy || !validSelectedFile();
  els.create.innerHTML = 'Tạo job <span>→</span>';
}
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options });
  const type = response.headers.get('content-type') || '';
  const value = type.includes('application/json') ? await response.json() : {};
  if (!response.ok) throw new Error(value.error || `Yêu cầu lỗi (${response.status})`);
  return value;
}
function updateHealth(health) {
  const pipeline = health?.pipeline || {};
  const ready = Boolean(health?.ready);
  pipelineReady = ready;
  els.dot.className = `status-dot ${ready ? 'ready' : 'offline'}`;
  els.status.textContent = ready ? 'Engine và pipeline sẵn sàng' : 'Engine đang thiếu tài nguyên hoặc model';
  const resources = health?.resources || {};
  const reported = health?.limits || {};
  const ramFree = Number(resources.ram_free_mib) || 0;
  const vramFree = Number(resources.vram_free_mib) || 0;
  const ramFloor = Number(resources.min_free_ram_mib) || 8192;
  const gpuFloor = Number(resources.min_free_vram_mib) || 1800;
  const vramText = resources.vram_free_mib == null
    ? `CPU fallback (RAM min ${prettyBytes((Number(resources.cpu_fallback_ram_mib) || 12288) * 1048576)})`
    : `${prettyBytes(vramFree * 1048576)} VRAM trống (GPU min ${prettyBytes(gpuFloor * 1048576)}${vramFree < gpuFloor ? ', CPU fallback' : ''})`;
  els.resources.textContent = `RAM ${prettyBytes(ramFree * 1048576)} trống (min ${prettyBytes(ramFloor * 1048576)}) · ${vramText} · Đĩa ${prettyBytes((Number(resources.disk_free_mib) || 0) * 1048576)}`;
  limits.maxBytes = Number(reported.max_upload_bytes) || 0;
  limits.maxSeconds = Number(reported.max_video_seconds) || 7200;
  mp4DeliveryRestricted = location.hostname.endsWith('.trycloudflare.com') && !reported.mp4_delivery_via_tunnel;
  els.limitDuration.textContent = `30 giây – ${prettyDuration(limits.maxSeconds)}`;
  els.limitUpload.textContent = limits.maxBytes ? prettyBytes(limits.maxBytes) : 'Tạm dừng nhận file';
  els.limitRuntime.textContent = `Tối đa ${prettyDuration(Number(reported.max_runtime_seconds) || 21600)}`;
  els.limitDisk.textContent = `${prettyBytes((Number(resources.disk_free_mib) || 0) * 1048576)} trống · giữ lại ${prettyBytes((Number(reported.min_free_disk_mib) || 10240) * 1048576)}`;
  els.limitOutput.textContent = 'Giữ đến khi bạn xóa';
  const missing = [];
  if (!pipeline.video_tools) missing.push('ffmpeg/ffprobe');
  if (!pipeline.speech_recognition) missing.push('Faster-Whisper hoặc model nhận diện');
  if (!pipeline.translation) missing.push('Ollama hoặc model dịch');
  if (!pipeline.vietnamese_voice) missing.push('model giọng tiếng Việt');
  if (!resources.ram || resources.ram !== 'safe') missing.push('RAM dưới ngưỡng an toàn');
  if (!resources.vram || resources.vram !== 'safe') missing.push('VRAM dưới ngưỡng an toàn');
  if (!resources.disk || resources.disk !== 'safe') missing.push('dung lượng đĩa dưới ngưỡng an toàn');
  if (!ready) showNotice(`Chưa nhận job mới. Máy chủ đang thiếu: ${[...new Set(missing)].join(', ')}.`, 'warning');
  syncCreateButton();
}
function chooseFile(file) {
  selectedFile = file || null;
  if (!file) {
    els.fileTitle.textContent = 'Chọn file video';
    els.fileSubtitle.textContent = `MP4, MOV, MKV hoặc WEBM · tối đa ${limits.maxBytes ? prettyBytes(limits.maxBytes) : 'tạm dừng'}`;
    syncCreateButton();
    return;
  }
  els.fileTitle.textContent = file.name;
  els.fileSubtitle.textContent = `${prettyBytes(file.size)} · kiểm tra thời lượng sau khi tải lên`;
  const okay = validSelectedFile();
  syncCreateButton();
  if (!okay) showNotice(`Chọn MP4, MOV, MKV hoặc WEBM từ 64 KiB đến ${limits.maxBytes ? prettyBytes(limits.maxBytes) : 'giới hạn hiện tại'} theo dung lượng đĩa.`, 'warning');
  else showNotice('Máy chủ kiểm tra thời lượng 30 giây–2 giờ bằng ffprobe trước khi nhận job.', 'info');
}
function formatTime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${m}:${String(s % 60).padStart(2, '0')}`;
}
function errorText(code) {
  const map = {
    PIPELINE_FAILED: 'Job chưa hoàn tất. Lỗi chi tiết của máy chủ không được công khai.',
    PIPELINE_UNAVAILABLE: 'Model hoặc tài nguyên của máy chủ không còn sẵn sàng.',
    JOB_TIMEOUT: 'Job vượt quá thời gian xử lý an toàn đã cấp theo thời lượng video.',
    JOB_STORAGE_LIMIT: 'Job chạm ngân sách đĩa đã cấp cho file trung gian và output.',
    RESOURCE_PRESSURE: 'Job dừng để bảo vệ RAM của máy chủ.',
  };
  return map[code] || 'Job chưa hoàn tất. Hãy thử lại sau.';
}
function renderJob(job) {
  currentJob = job;
  if (!job) {
    els.empty.hidden = false;
    els.content.hidden = true;
    els.heading.textContent = 'Chưa có job';
    els.preview.hidden = true;
    els.logPanel.hidden = true;
    return;
  }
  els.empty.hidden = true;
  els.content.hidden = false;
  els.heading.textContent = job.title || 'Video';
  els.duration.textContent = job.duration ? `${formatTime(job.duration)} · ${job.sentences || 0} câu` : '';
  els.statusChip.textContent = ({ queued: 'Đang chờ', running: 'Đang xử lý', cancel_requested: 'Đang hủy', cancelled: 'Đã hủy', completed: 'Hoàn tất', blocked: 'Đã dừng' })[job.status] || job.status;
  els.statusChip.className = `job-status ${job.status || ''}`;
  const queueEta = job.estimated_wait_seconds == null ? '' : job.estimated_wait_seconds === 0
    ? ' · sắp được xử lý'
    : ` · ${job.estimated_wait_basis === 'runtime_limit' ? 'chờ bảo thủ' : 'chờ ước tính'} ${prettyDuration(job.estimated_wait_seconds)}`;
  const estimateNote = job.estimated_wait_basis === 'runtime_limit'
    ? ' · chưa có mẫu hoàn tất'
    : job.estimate_sample_count > 0 ? ` · từ ${job.estimate_sample_count} job` : '';
  const processEta = job.estimated_processing_seconds == null ? '' : ` · xử lý khoảng ${prettyDuration(job.estimated_processing_seconds)}`;
  const queueLabel = job.status === 'queued' ? `Vị trí ${job.queue_position || '—'}${queueEta}${estimateNote}${processEta}` : '';
  els.stageCopy.textContent = job.status === 'completed' ? 'Xử lý hoàn tất' : job.status === 'queued' ? queueLabel : job.stage_label || 'Đang xử lý';
  els.jobPercent.textContent = `${job.progress || 0}%`;
  els.jobBar.style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
  els.stages.replaceChildren();
  stageNames.forEach((name, index) => {
    const key = ['download','audio','transcribe','translate','subtitles','tts','timing','render'][index];
    const state = job.stages?.[key] || 'waiting';
    const item = document.createElement('div');
    item.className = `stage ${state}`;
    const number = document.createElement('span'); number.className = 'stage-num'; number.textContent = String(index + 1).padStart(2, '0');
    const label = document.createElement('span'); label.textContent = name;
    item.append(number, label); els.stages.append(item);
  });
  els.error.hidden = !job.error;
  if (job.error) els.error.textContent = errorText(job.error);
  els.cancel.hidden = !job.can_cancel;
  els.deleteJob.hidden = job.can_cancel || ['running','queued','cancel_requested'].includes(job.status);
  els.actions.hidden = !job.output_ready;
  els.downloadMp4.hidden = mp4DeliveryRestricted;
  els.mp4DeliveryNote.hidden = !mp4DeliveryRestricted || !job.output_ready;
  els.preview.hidden = !job.output_ready;
  els.video.hidden = mp4DeliveryRestricted;
  els.videoDeliveryNote.hidden = !mp4DeliveryRestricted || !job.output_ready;
  els.logPanel.hidden = false;
  if (job.output_ready) {
    const outputUrl = `/api/public/jobs/${encodeURIComponent(job.id)}/output`;
    const srtUrl = `/api/public/jobs/${encodeURIComponent(job.id)}/subtitles?download=1`;
    els.downloadMp4.href = `${outputUrl}?download=1`;
    els.downloadSrt.href = srtUrl;
    if (!mp4DeliveryRestricted) {
      const video = document.createElement('video'); video.controls = true; video.preload = 'metadata';
      video.src = outputUrl;
      const track = document.createElement('track'); track.kind = 'subtitles'; track.src = `/api/public/jobs/${encodeURIComponent(job.id)}/vtt`; track.srclang = 'vi'; track.label = 'Tiếng Việt'; track.default = true;
      video.append(track); els.video.replaceChildren(video);
    } else {
      els.video.replaceChildren();
    }
    loadSegments(job.id);
  }
  loadEvents(job.id);
}
async function loadSegments(jobId) {
  if (lastSegments === jobId) return;
  try {
    const rows = await api(`/api/public/jobs/${encodeURIComponent(jobId)}/segments`);
    lastSegments = jobId;
    els.transcript.replaceChildren();
    if (!rows.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'Không có phụ đề.'; els.transcript.append(p); return; }
    for (const row of rows) {
      const card = document.createElement('article'); card.className = 'transcript-item';
      const time = document.createElement('time'); time.className = 'transcript-time'; time.textContent = `${formatTime(row.start)} – ${formatTime(row.end)}`;
      const text = document.createElement('div'); text.className = 'transcript-text'; text.textContent = row.target || '';
      card.append(time, text); els.transcript.append(card);
    }
  } catch (_) { }
}
async function loadEvents(jobId) {
  if (lastEvents === jobId) return;
  try {
    const result = await api(`/api/public/jobs/${encodeURIComponent(jobId)}/log`);
    lastEvents = jobId;
    els.events.replaceChildren();
    for (const event of result.events || []) {
      const li = document.createElement('li');
      const time = document.createElement('time'); time.textContent = new Date(event.created_at * 1000).toLocaleTimeString('vi-VN');
      const message = document.createElement('span'); message.textContent = event.message;
      li.append(time, message); els.events.append(li);
    }
  } catch (_) { }
}
async function refresh() {
  try {
    const state = await api('/api/public/state');
    updateHealth(state.health);
    const jobs = state.jobs || [];
    const previousId = currentJob?.id;
    const previousStatus = currentJob?.status;
    const job = currentJob ? jobs.find(item => item.id === currentJob.id) : jobs[0];
    if (job && (job.id !== previousId || job.status !== previousStatus)) {
      lastEvents = '';
      if (job.id !== previousId) lastSegments = '';
    }
    renderJob(job || null);
  } catch (error) {
    els.dot.className = 'status-dot offline';
    els.status.textContent = 'Không kết nối được với engine';
    showNotice(error.message || 'Không kết nối được.', 'warning');
  }
  window.setTimeout(refresh, currentJob && ['running','queued'].includes(currentJob.status) ? 2500 : 8000);
}
function setUploadProgress(received, total) {
  const percent = Math.min(100, Math.round(received / total * 100));
  els.uploadPercent.textContent = `${percent}%`;
  els.uploadBar.style.width = `${percent}%`;
  els.uploadLabel.textContent = `Đang tải lên ${prettyBytes(received)} / ${prettyBytes(total)}`;
}
async function fileFingerprint(file) {
  const sampleBytes = 1024 * 1024;
  const first = new Uint8Array(await file.slice(0, sampleBytes).arrayBuffer());
  const last = new Uint8Array(await file.slice(Math.max(0, file.size - sampleBytes), file.size).arrayBuffer());
  const metadata = new TextEncoder().encode(`${file.name}\0${file.size}\0${file.lastModified}\0`);
  const material = new Uint8Array(metadata.length + first.length + last.length);
  material.set(metadata, 0); material.set(first, metadata.length); material.set(last, metadata.length + first.length);
  const digest = await crypto.subtle.digest('SHA-256', material);
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}
async function uploadProgress(uploadId) {
  return api(`/api/public/uploads/${encodeURIComponent(uploadId)}`);
}
async function createJob() {
  if (busy) return;
  if (!selectedFile) return;
  busy = true; els.create.disabled = true; els.uploadProgress.hidden = false; showNotice('Đang upload video theo từng chunk.', 'info');
  try {
    const fingerprint = await fileFingerprint(selectedFile);
    const start = await api('/api/public/uploads/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: selectedFile.name, size: selectedFile.size, fingerprint })
    });
    let offset = Number(start.received) || 0;
    const saved = { upload: start.upload, name: selectedFile.name, size: selectedFile.size, fingerprint };
    sessionStorage.setItem(`studio-upload:${fingerprint}`, JSON.stringify(saved));
    setUploadProgress(offset, selectedFile.size);
    while (offset < selectedFile.size) {
      const chunkOffset = offset;
      const chunk = selectedFile.slice(chunkOffset, Math.min(selectedFile.size, chunkOffset + start.chunk_bytes));
      let sent = false;
      let lastError = null;
      for (let attempt = 0; attempt < 4 && !sent; attempt++) {
        try {
          const response = await fetch(`/api/public/uploads/${encodeURIComponent(start.upload)}`, {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
            headers: { 'Content-Type': 'application/octet-stream', 'X-Offset': String(chunkOffset) },
            body: chunk, signal: AbortSignal.timeout(90000)
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `Upload lỗi (${response.status})`);
          offset = Number(result.received) || offset + chunk.size;
          sent = true;
        } catch (error) {
          lastError = error;
          await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)));
          const progress = await uploadProgress(start.upload);
          offset = Number(progress.received) || 0;
          sent = offset > chunkOffset;
          if (!sent && attempt === 3) throw lastError;
        }
      }
      if (!sent && lastError) throw lastError;
      saved.received = offset;
      sessionStorage.setItem(`studio-upload:${fingerprint}`, JSON.stringify(saved));
      setUploadProgress(offset, selectedFile.size);
    }
    const job = await api('/api/public/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ upload: start.upload }) });
    currentJob = job; lastSegments = ''; lastEvents = '';
    sessionStorage.removeItem(`studio-upload:${fingerprint}`);
    renderJob(job); showNotice('Đã tạo job sau khi kiểm tra toàn bộ video bằng ffprobe.', 'info');
    selectedFile = null; els.file.value = ''; els.fileTitle.textContent = 'Chọn file video'; els.fileSubtitle.textContent = `MP4, MOV, MKV hoặc WEBM · tối đa ${prettyBytes(limits.maxBytes)}`;
    els.uploadProgress.hidden = true;
  } catch (error) {
    showNotice(`${error.message || 'Không tạo được job.'} Nếu mạng bị ngắt, chọn lại đúng file này để tiếp tục upload đã lưu.`, 'warning');
  } finally {
    busy = false;
    syncCreateButton();
    await refresh();
  }
}
els.file.addEventListener('change', () => chooseFile(els.file.files?.[0]));
els.create.addEventListener('click', createJob);
els.cancel.addEventListener('click', async () => {
  if (!currentJob?.id || !currentJob.can_cancel) return;
  els.cancel.disabled = true;
  try {
    await api(`/api/public/jobs/${encodeURIComponent(currentJob.id)}/cancel`, { method: 'POST' });
    showNotice('Đã gửi yêu cầu hủy job.', 'info');
    await refresh();
  } catch (error) {
    showNotice(error.message || 'Không hủy được job.', 'warning');
  } finally {
    els.cancel.disabled = false;
  }
});
els.deleteJob.addEventListener('click', async () => {
  if (!currentJob?.id || currentJob.can_cancel) return;
  if (!window.confirm('Xóa job, phụ đề và output MP4 của phiên này?')) return;
  els.deleteJob.disabled = true;
  try {
    await api(`/api/public/jobs/${encodeURIComponent(currentJob.id)}/delete`, { method: 'POST' });
    currentJob = null; lastSegments = ''; lastEvents = '';
    renderJob(null); showNotice('Đã xóa job và giải phóng dung lượng.', 'info');
    await refresh();
  } catch (error) {
    showNotice(error.message || 'Không xóa được job.', 'warning');
  } finally {
    els.deleteJob.disabled = false;
  }
});
for (const event of ['dragenter','dragover']) els.dropzone.addEventListener(event, e => { e.preventDefault(); els.dropzone.classList.add('dragging'); });
for (const event of ['dragleave','drop']) els.dropzone.addEventListener(event, e => { e.preventDefault(); els.dropzone.classList.remove('dragging'); });
els.dropzone.addEventListener('drop', e => chooseFile(e.dataTransfer?.files?.[0]));
api('/api/public/session').catch(() => {});
refresh();
