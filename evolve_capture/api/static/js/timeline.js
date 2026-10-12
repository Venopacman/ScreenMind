//  TIMELINE VIEW
// ══════════════════════════════════════════════════════════
async function renderTimeline(el) {
  el.innerHTML = `
    <div class="date-nav" style="margin-bottom:20px">
      <button class="btn btn-ghost btn-sm" id="prev-day">\u25c0</button>
      <input type="date" id="timeline-date" value="${currentDate}">
      <button class="btn btn-ghost btn-sm" id="next-day">\u25b6</button>
      <span style="margin-left:12px;color:var(--text-muted);font-size:0.85rem" id="tl-count"></span>
      <button class="btn btn-ghost btn-sm" id="clear-timeline" style="margin-left:auto;color:#ef4444;font-size:0.8rem" title="Clear today's timeline">🗑 Clear Timeline</button>
    </div>
    <div class="timeline" id="timeline-list"><div class="spinner"></div></div>`;
  $('#timeline-date').addEventListener('change', e => { currentDate = e.target.value; loadTimeline(); });
  $('#prev-day').addEventListener('click', () => shiftDate(-1));
  $('#next-day').addEventListener('click', () => shiftDate(1));
  $('#clear-timeline').addEventListener('click', confirmClearTimeline);
  loadTimeline();
  // Auto-refresh timeline every 30s
  if (window._tlRefresh) clearInterval(window._tlRefresh);
  window._tlRefresh = setInterval(() => { if (currentView === 'timeline') loadTimeline(true); }, 30000);
}

function shiftDate(days) {
  const d = new Date(currentDate); d.setDate(d.getDate() + days);
  currentDate = d.toISOString().split('T')[0];
  $('#timeline-date').value = currentDate;
  loadTimeline();
}

async function loadTimeline(silent = false) {
  const list = $('#timeline-list');
  if (!silent) list.innerHTML = '<div class="spinner"></div>';
  try {
    const data = await api(`/api/timeline?date=${currentDate}`);
    const acts = data.activities || [];

    // Also fetch meetings for this date
    let meetings = [];
    try {
      const mData = await api(`/api/meetings?date=${currentDate}`);
      meetings = mData.meetings || [];
    } catch { /* meetings endpoint not available */ }

    const totalCount = acts.length + meetings.length;
    $('#tl-count').textContent = `${totalCount} activities` + (meetings.length ? ` · ${meetings.length} meeting${meetings.length > 1 ? 's' : ''}` : '');

    if (!acts.length && !meetings.length) {
      list.innerHTML = `<div class="onboarding">
        <h2>🚀 Evolve Capture is Running!</h2>
        <div class="subtitle">The first capture is coming soon...</div>
        <div class="feature-grid">
          <div class="feature-card"><div class="fc-icon">📋</div><div class="fc-title">Timeline</div><div class="fc-desc">Every captured frame with app, window, page and screen text</div></div>
          <div class="feature-card"><div class="fc-icon">🔍</div><div class="fc-title">Search</div><div class="fc-desc">Keyword search over screen text, apps and actions</div></div>
          <div class="feature-card"><div class="fc-icon">📊</div><div class="fc-title">Analytics</div><div class="fc-desc">Time per category and app</div></div>
          <div class="feature-card"><div class="fc-icon">🎙️</div><div class="fc-title">Meetings</div><div class="fc-desc">Calls with app, start and end</div></div>
          <div class="feature-card"><div class="fc-icon">🛡️</div><div class="fc-title">100% Local</div><div class="fc-desc">No cloud, no telemetry. Data leaves only when you export it.</div></div>
        </div>
        <div class="waiting-pulse"><span class="pulse-dot"></span> Waiting for first capture...</div>
      </div>`;
      return;
    }
    // One feed, newest first (same order as /api/timeline). Both times are
    // local ISO strings, so string compare sorts them. A meeting older than
    // the oldest loaded activity lands at the end.
    const items = [
      ...meetings.map(m => ({ t: m.start_time || '', m })),
      ...acts.map(a => ({ t: a.timestamp || '', a })),
    ].sort((x, y) => (x.t < y.t ? 1 : x.t > y.t ? -1 : 0));
    list.innerHTML = items.map((it, i) => it.m ? meetingCard(it.m) : timelineCard(it.a, i)).join('');
  } catch (err) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Error</div><div>${err.message}</div></div>`;
  }
}

function meetingCard(m) {
  const startTime = formatTime(m.start_time);
  const endTime = m.end_time ? formatTime(m.end_time) : 'ongoing';
  const duration = m.duration_minutes ? `${Math.round(m.duration_minutes)} min` : '';
  // Only calls from before Gemma was removed have a summary and transcript
  const summaryText = _tlEscape(m.summary || '');
  const transcriptText = _tlEscape(m.transcript || '');
  // First meaningful line of the summary, else the call window's title
  const previewLine = summaryText.split('\n').find(l => l.trim() && !l.startsWith('⏳'))
    || _tlEscape(m.window_title || 'Call tracked');
  const tid = `tl-mtg-transcript-${m.id}`;
  const sid = `tl-mtg-summary-${m.id}`;
  return `
    <div class="timeline-item tl-meeting-card" onclick="toggleTlMeeting(${m.id})">
      <div class="thumb" style="display:flex;align-items:center;justify-content:center;font-size:2rem;background:rgba(236,72,153,0.1);cursor:pointer">🎙️</div>
      <div class="info" style="flex:1;min-width:0">
        <div class="top">
          <span class="time">${startTime}</span>
          <span class="app-name">Meeting — ${(m.app_name || 'Unknown').replace(/</g, '&lt;')}</span>
          <span class="badge badge-other" style="background:rgba(236,72,153,0.15);color:#ec4899">meeting</span>
        </div>
        <div class="summary">${startTime} – ${endTime}${duration ? ' · ' + duration : ''} · ${previewLine.substring(0, 80)}${previewLine.length > 80 ? '...' : ''}</div>
        <div class="tl-meeting-detail" id="${sid}" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.06)">
          <div style="white-space:pre-wrap;font-size:0.82rem;line-height:1.6;color:var(--text-secondary)">${summaryText}</div>
          ${transcriptText && transcriptText !== '(No speech detected)' ? `
            <button class="meeting-transcript-toggle" style="margin-top:8px" onclick="event.stopPropagation(); var t=document.getElementById('${tid}'); t.classList.toggle('open'); this.textContent = t.classList.contains('open') ? '▲ Hide transcript' : '▼ Show full transcript'">▼ Show full transcript</button>
            <div class="meeting-transcript" id="${tid}">${transcriptText}</div>
          ` : ''}
        </div>
      </div>
      <div style="color:var(--text-muted);font-size:0.78rem;white-space:nowrap;margin-left:8px">${duration}</div>
    </div>`;
}

window.toggleTlMeeting = function(id) {
  const el = document.getElementById('tl-mtg-summary-' + id);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
};

function timelineCard(a, i) {
  const time = formatTime(a.timestamp);
  const cat = a.category || 'other';
  const thumb = a.screenshot_url ? `<img class="thumb" src="${a.screenshot_url}" loading="lazy" onclick="openModal('${a.screenshot_url}', ${a.id})" alt="">` : '<div class="thumb"></div>';

  // Analysis method badge — color-coded
  const method = a.analysis_method || '';
  const methodColor = method ? methodColorFor(method) : '';
  const methodBadge = method ? ` <span style="background:${methodColor}18;color:${methodColor};border:1px solid ${methodColor}33;font-size:0.65rem;padding:1px 6px;border-radius:8px;font-weight:500">${method}</span>` : '';

  // UI events recorded before this frame (clicks, typed text, app switches)
  const actionLines = (a.user_actions || '').split('\n').filter(Boolean).map(l => l.replace(/^- /, ''));
  const userActions = actionLines.length ? `<details class="user-actions" onclick="event.stopPropagation()"><summary>Actions (${actionLines.length})</summary><ul>${actionLines.map(l => `<li>${_tlEscape(l)}</li>`).join('')}</ul></details>` : '';

  return `
    <div class="timeline-item" style="animation-delay:${i * 0.06}s">
      ${thumb}
      <div class="info">
        <div class="top">
          <span class="time">${time}</span>
          <span class="app-name">${_tlEscape(a.app_name || a.detected_app || 'Unknown')}</span>
          <span class="badge badge-${cat}">${cat}</span>${methodBadge}
        </div>
        <div class="summary">${_tlEscape(frameLine(a))}</div>
        ${a.active_url ? `<div style="margin-top:2px"><a href="${a.active_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="font-size:0.7rem;color:#60a5fa;text-decoration:none;opacity:0.8;word-break:break-all" title="${a.active_url}">🔗 ${(() => { try { return new URL(a.active_url).hostname } catch(e) { return a.active_url.substring(0, 40) } })()}</a></div>` : ''}
        ${userActions}
      </div>
      <div class="card-menu-wrap">
        <button class="card-menu-trigger" onclick="event.stopPropagation(); toggleCardMenu(this)" title="Actions">⋮</button>
        <div class="card-menu">
          <button class="menu-item delete-item" onclick="event.stopPropagation(); deleteActivity(${a.id}, this)">
            <span class="menu-icon">✕</span> Delete
          </button>
        </div>
      </div>
    </div>`;
}

function _tlEscape(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Three-dot menu toggle ─────────────────────────────────
window.toggleCardMenu = function(trigger) {
  const menu = trigger.nextElementSibling;
  const isOpen = menu.classList.contains('open');
  // Close all other open menus first
  document.querySelectorAll('.card-menu.open').forEach(function(m) { m.classList.remove('open'); });
  document.querySelectorAll('.card-menu-trigger.active').forEach(function(t) { t.classList.remove('active'); });
  if (!isOpen) {
    menu.classList.add('open');
    trigger.classList.add('active');
  }
};

// Close menus when clicking outside
document.addEventListener('click', function() {
  document.querySelectorAll('.card-menu.open').forEach(function(m) { m.classList.remove('open'); });
  document.querySelectorAll('.card-menu-trigger.active').forEach(function(t) { t.classList.remove('active'); });
});

window.deleteActivity = async function(id, btn) {
  if (!btn.classList.contains('confirm-delete')) {
    btn.innerHTML = '<span class="menu-icon">⚠</span> Confirm Delete';
    btn.classList.add('confirm-delete');
    setTimeout(function() {
      btn.innerHTML = '<span class="menu-icon">✕</span> Delete';
      btn.classList.remove('confirm-delete');
    }, 3000);
    return;
  }
  const card = btn.closest('.timeline-item');
  card.style.transform = 'translateX(100px)';
  card.style.opacity = '0';
  card.style.transition = 'all 0.3s ease';
  try {
    const r = await fetch(`/api/activities/${id}`, { method: 'DELETE' });
    if (!r.ok) throw new Error('Failed');
    setTimeout(function() { card.remove(); }, 300);
    showToast('Activity deleted', 'success');
    const count = document.querySelectorAll('.timeline-item').length - 1;
    const countEl = $('#tl-count');
    if (countEl) countEl.textContent = `${count} activities`;
  } catch (err) {
    card.style.transform = ''; card.style.opacity = '';
    showToast('Delete failed', 'warning');
  }
};

// ── Clear Timeline (with confirmation) ────────────────────
function confirmClearTimeline() {
  const container = $('#toast-container');
  // Remove any existing confirm toast
  const existing = document.querySelector('.confirm-toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.className = 'toast confirm-toast warning';
  toast.style.cssText = 'display:flex;flex-direction:column;gap:10px;max-width:320px;';
  toast.innerHTML = `
    <div style="font-weight:600">Clear timeline for ${currentDate}?</div>
    <div style="font-size:0.85rem;opacity:0.8">This will permanently delete all screenshots and activity data for this day.</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-ghost btn-sm" onclick="this.closest('.confirm-toast').remove()" style="color:var(--text-muted)">Cancel</button>
      <button class="btn btn-sm" onclick="executeClearTimeline()" style="background:#ef4444;color:#fff;border:none;padding:6px 16px;border-radius:6px;cursor:pointer">Yes, Delete</button>
    </div>`;
  container.appendChild(toast);
}

window.executeClearTimeline = async function() {
  const toast = document.querySelector('.confirm-toast');
  if (toast) toast.remove();
  try {
    const r = await fetch(`/api/timeline/clear?date=${currentDate}`, { method: 'DELETE' });
    const data = await r.json();
    showToast(`Cleared ${data.deleted} activities for ${data.date}`, 'success');
    loadTimeline();
  } catch (err) {
    showToast('Failed to clear timeline', 'warning');
  }
};

// ══════════════════════════════════════════════════════════
