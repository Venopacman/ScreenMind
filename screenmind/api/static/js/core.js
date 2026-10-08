/* ═══════════════════════════════════════════════════════════
   ScreenMind — Main Application
   SPA with physics-inspired transitions & micro-interactions
   ═══════════════════════════════════════════════════════════ */

const API = '';
// Use local date (not UTC) to avoid timezone shift issues
const _now = new Date();
let currentDate = `${_now.getFullYear()}-${String(_now.getMonth()+1).padStart(2,'0')}-${String(_now.getDate()).padStart(2,'0')}`;
let currentView = 'timeline';

// ── First-Run Check ────────────────────────────────────────
async function _checkAuth() {
  try {
    var r = await fetch('/api/auth/status');
    var data = await r.json();
    if (data.first_run) {
      document.getElementById('welcome-screen').style.display = 'flex';
      document.getElementById('app').style.display = 'none';
      return true;
    }
  } catch(e) {}
  return false;
}

window.completeSetup = async function() {
  try {
    await fetch('/api/auth/setup-complete', { method: 'POST' });
  } catch(e) {}
  document.getElementById('welcome-screen').style.display = 'none';
  document.getElementById('app').style.display = '';
  _initApp();
};

window.toggleIncognito = async function() {
  try {
    var r = await fetch('/api/incognito/toggle', { method: 'POST' });
    var data = await r.json();
    var btn = document.getElementById('incognito-btn');
    if (data.incognito) {
      btn.style.background = 'rgba(239,68,68,0.2)';
      btn.title = 'Incognito ON — click to disable';
      showToast('🕶️ Incognito mode — no recording', 'warning');
    } else {
      btn.style.background = '';
      btn.title = 'Incognito Mode';
      showToast('Incognito mode off', 'success');
    }
  } catch(e) {}
};

// ── API Client ────────────────────────────────────────────
async function api(path, opts) {
  const r = await fetch(API + path, opts || {});
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    const e = new Error(detail.error || detail.detail || `API ${r.status}`);
    e.detail = detail.error || detail.detail;
    throw e;
  }
  return r.json();
}
async function apiPost(path) {
  return api(path, { method: 'POST' });
}
async function apiDelete(path) {
  return api(path, { method: 'DELETE' });
}

// ── Helpers ───────────────────────────────────────────────
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }
function formatTime(ts) {
  return new Date(ts).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });
}
function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
// analysis_method badge colour. Old labels (full:fast, cache:minor, reanalyze) stay on old frames.
function methodColorFor(method) {
  const m = (method || '').replace(/^backfill:/, '');
  if (method.startsWith('backfill:')) return '#22d3ee';
  if (m === 'rules') return '#a78bfa';
  if (m.startsWith('cache:')) return '#34d399';
  if (m.startsWith('rule:')) return '#94a3b8';
  if (m.startsWith('full')) return '#c084fc';
  if (m === 'reanalyze') return '#f97316';
  return '#6b7280';
}
// One line of text for a frame: its window title, else the old Gemma
// summary, else the start of its screen text.
function frameLine(a) {
  if (a.window_title) return a.window_title;
  if (a.summary) return a.summary;
  const text = (a.ocr_text || '').replace(/\s+/g, ' ').trim();
  return text ? (text.length > 140 ? text.substring(0, 140) + '…' : text) : '';
}
function catColor(cat) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--cat-${cat || 'other'}`).trim() || '#64748b';
}

// ── Modal (Enhanced: split view with OCR + details) ───────
window.openModal = async function(src, activityId) {
  $('#modal-img').src = src;
  $('#modal').classList.add('visible');
  // Fetch full activity details
  if (activityId) {
    try {
      const a = await api(`/api/activity/${activityId}`);
      const time = new Date(a.timestamp).toLocaleString();
      const cat = a.category || 'other';
      const method = a.analysis_method || 'unknown';
      const methodColor = methodColorFor(method);
      const app = a.app_name || a.detected_app || 'Unknown';

      $('#modal-meta').innerHTML = `
        <div class="meta-row"><span class="time">${time}</span></div>
        <div class="meta-row"><strong>${escapeHtml(app)}</strong> <span class="badge badge-${cat}">${cat}</span> <span class="badge" style="background:${methodColor}22;color:${methodColor};border:1px solid ${methodColor}44;font-size:0.7rem;padding:2px 8px;border-radius:10px;margin-left:6px">${method}</span></div>
        ${a.active_url ? `<div style="margin-top:6px"><a href="${a.active_url}" target="_blank" rel="noopener" style="font-size:0.75rem;color:#60a5fa;text-decoration:none;word-break:break-all;display:inline-flex;align-items:center;gap:4px" title="${a.active_url}">🔗 ${a.active_url.length > 80 ? a.active_url.substring(0, 80) + '…' : a.active_url}</a></div>` : ''}`;
      // Older frames keep their Gemma summary; new frames show the window title
      $('#modal-summary').textContent = a.window_title || a.details || a.summary || '(no window title)';

      $('#modal-ocr').textContent = a.organized_text || a.ocr_text || a.scene_description || 'No screen text';
    } catch { $('#modal-summary').textContent = 'Unable to load details'; }
  }
};
window.closeModal = function(e) {
  if (e && e.target && e.target !== $('#modal')) return;
  $('#modal').classList.remove('visible');
};
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Toast Notifications ───────────────────────────────────
window.showToast = function(message, type = 'info') {
  const container = $('#toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.classList.add('exit'); setTimeout(() => toast.remove(), 300); }, 3000);
};

// ── Text Highlighting ─────────────────────────────────────
function highlightText(text, query) {
  if (!text || !query) return text || '';
  const words = query.trim().split(/\s+/).filter(w => w.length > 2);
  if (!words.length) return text;
  const pattern = words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  return text.replace(new RegExp(`(${pattern})`, 'gi'), '<mark>$1</mark>');
}

// ── Sliding Nav Indicator ─────────────────────────────────
function moveIndicator(btn) {
  const indicator = $('#nav-indicator');
  const nav = $('#sidebar-nav');
  const navRect = nav.getBoundingClientRect();
  const btnRect = btn.getBoundingClientRect();
  const y = btnRect.top - navRect.top;
  indicator.style.transform = `translateY(${y}px)`;
  indicator.style.height = `${btnRect.height}px`;
}

// ── Router with animated transitions ──────────────────────
function navigate(view) {
  currentView = view;
  window.location.hash = view;

  // Update nav
  const btns = $$('.nav-item');
  btns.forEach(n => n.classList.toggle('active', n.dataset.view === view));
  const activeBtn = $(`[data-view="${view}"]`);
  if (activeBtn) moveIndicator(activeBtn);

  // Update header
  const titles = { timeline:'Timeline', search:'Search', analytics:'Analytics', meetings:'Meetings', settings:'Settings' };
  $('#page-title').textContent = titles[view] || view;

  const el = $('#content');

  // Clear initial loading spinner on first navigate
  const spinner = el.querySelector(':scope > .spinner');
  if (spinner) spinner.remove();

  let ephemeral = el.querySelector('.view-ephemeral');
  if (!ephemeral) {
    ephemeral = document.createElement('div');
    ephemeral.className = 'view-ephemeral';
    el.appendChild(ephemeral);
  }
  ephemeral.innerHTML = '';
  const wrapper = document.createElement('div');
  wrapper.className = 'view-enter';
  ephemeral.appendChild(wrapper);

  const fns = { timeline: renderTimeline, search: renderSearch, analytics: renderAnalytics, meetings: renderMeetings, settings: renderSettings };
  (fns[view] || renderTimeline)(wrapper);
}

$('#sidebar-nav').addEventListener('click', e => {
  const btn = e.target.closest('.nav-item');
  if (btn) navigate(btn.dataset.view);
});

// ── Status Polling ────────────────────────────────────────
async function pollStatus() {
  try {
    const s = await api('/api/status');
    const dot = $('#status-dot');
    const txt = $('#status-text');
    const pauseBtn = $('#pause-btn');
    const pauseIcon = $('#pause-icon');
    const pauseLabel = $('#pause-label');
    if (s.capture?.paused) {
      dot.className = 'status-dot paused'; txt.textContent = 'Ready';
      if (pauseBtn) { pauseBtn.classList.add('paused'); pauseIcon.textContent = '\u25b6'; pauseLabel.textContent = 'Start Capturing'; }
    } else {
      const count = s.capture?.captures || 0;
      dot.className = 'status-dot'; txt.textContent = `Capturing (${count})`;
      if (pauseBtn) { pauseBtn.classList.remove('paused'); pauseIcon.textContent = '\u23f8'; pauseLabel.textContent = 'Stop Capturing'; }
    }
  } catch { $('#status-text').textContent = 'Offline'; $('#status-dot').className = 'status-dot error'; }
}

// ── Start / Stop Capture Toggle ───────────────────────────
window.toggleCapture = async function() {
  const btn = $('#pause-btn');
  const isPaused = btn.classList.contains('paused');
  try {
    await apiPost(isPaused ? '/api/capture/resume' : '/api/capture/pause');
    showToast(isPaused ? 'Capture started!' : 'Capture stopped.', isPaused ? 'success' : 'warning');
    pollStatus();
  } catch (err) { showToast('Failed to toggle capture', 'warning'); }
};

// ── Animated Counter ──────────────────────────────────────
function animateValue(el, end, duration = 600) {
  const start = 0;
  const startTime = performance.now();
  const isFloat = String(end).includes('.');
  function update(now) {
    const t = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    const val = start + (end - start) * eased;
    el.textContent = isFloat ? val.toFixed(1) : Math.round(val);
    if (t < 1) requestAnimationFrame(update);
  }
  requestAnimationFrame(update);
}

// ── Status poll interval ──────────────────────────────────
let _pollIntervalId = null;
let _currentPollMs = 15000;
function _setPollInterval(ms) {
  if (ms === _currentPollMs && _pollIntervalId) return;
  _currentPollMs = ms;
  if (_pollIntervalId) clearInterval(_pollIntervalId);
  _pollIntervalId = setInterval(pollStatus, ms);
}

function _formatBytes(bytes) {
  if (bytes > 1024*1024*1024) return (bytes/(1024*1024*1024)).toFixed(1) + ' GB';
  if (bytes > 1024*1024) return Math.round(bytes/(1024*1024)) + ' MB';
  if (bytes > 1024) return Math.round(bytes/1024) + ' KB';
  return bytes + ' B';
}
