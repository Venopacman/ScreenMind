//  SEARCH VIEW
// ══════════════════════════════════════════════════════════
let searchTimeout;
let currentDateFilter = 'all'; // 'today' | '7d' | '30d' | 'all' | 'custom'

function getDateRange() {
  const today = new Date();
  const fmt = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;

  switch (currentDateFilter) {
    case 'today':
      return { date_from: fmt(today), date_to: fmt(today) };
    case '7d': {
      const from = new Date(today);
      from.setDate(from.getDate() - 6);
      return { date_from: fmt(from), date_to: fmt(today) };
    }
    case '30d': {
      const from = new Date(today);
      from.setDate(from.getDate() - 29);
      return { date_from: fmt(from), date_to: fmt(today) };
    }
    case 'custom': {
      const fromEl = document.getElementById('search-date-from');
      const toEl = document.getElementById('search-date-to');
      const dateFrom = fromEl?.value || '';
      const dateTo = toEl?.value || '';
      if (dateFrom || dateTo) {
        return {
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
        };
      }
      return {};
    }
    default: // 'all'
      return {};
  }
}

async function renderSearch(el) {
  const _n = new Date();
  const today = `${_n.getFullYear()}-${String(_n.getMonth()+1).padStart(2,'0')}-${String(_n.getDate()).padStart(2,'0')}`;

  el.innerHTML = `
    <div class="search-box">
      <span class="search-icon">🔍</span>
      <input type="text" id="search-input" placeholder="Search screen text and labels... (e.g. 'auth module')">
    </div>
    <div class="search-filters">
      <div class="search-date-pills">
        <button class="search-date-pill${currentDateFilter === 'all' ? ' active' : ''}" data-range="all">All Time</button>
        <button class="search-date-pill${currentDateFilter === 'today' ? ' active' : ''}" data-range="today">Today</button>
        <button class="search-date-pill${currentDateFilter === '7d' ? ' active' : ''}" data-range="7d">7 Days</button>
        <button class="search-date-pill${currentDateFilter === '30d' ? ' active' : ''}" data-range="30d">30 Days</button>
        <button class="search-date-pill${currentDateFilter === 'custom' ? ' active' : ''}" data-range="custom">📅 Custom</button>
      </div>
      <select id="search-category">
        <option value="">All Categories</option>
        <option value="coding">Coding</option>
        <option value="writing">Writing</option>
        <option value="browsing">Browsing</option>
        <option value="communication">Communication</option>
        <option value="design">Design</option>
        <option value="other">Other</option>
      </select>
    </div>
    <div class="search-date-range${currentDateFilter === 'custom' ? ' visible' : ''}" id="search-date-range-row">
      <label for="search-date-from">From</label>
      <input type="date" id="search-date-from" max="${today}">
      <span class="date-range-sep">→</span>
      <label for="search-date-to">To</label>
      <input type="date" id="search-date-to" value="${today}" max="${today}">
    </div>
    <div id="search-results">
      <div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-title">Search</div><div>Type words that were on screen, or in a label or a call transcript.</div></div>
    </div>`;

  // ── Event: search input ──
  const doSearchNow = () => {
    const q = $('#search-input').value;
    if (q.trim()) doSearch(q);
  };
  $('#search-input').addEventListener('input', e => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => doSearch(e.target.value), 400);
  });

  // ── Event: category change ──
  $('#search-category').addEventListener('change', doSearchNow);

  // ── Event: date pill clicks ──
  document.querySelectorAll('.search-date-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      currentDateFilter = pill.dataset.range;
      // Update active state
      document.querySelectorAll('.search-date-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      // Toggle custom date range row
      const rangeRow = document.getElementById('search-date-range-row');
      if (currentDateFilter === 'custom') {
        rangeRow.classList.add('visible');
      } else {
        rangeRow.classList.remove('visible');
      }
      doSearchNow();
    });
  });

  // ── Event: custom date inputs change ──
  const dateFrom = document.getElementById('search-date-from');
  const dateTo = document.getElementById('search-date-to');
  if (dateFrom) dateFrom.addEventListener('change', doSearchNow);
  if (dateTo) dateTo.addEventListener('change', doSearchNow);

  $('#search-input').focus();
}

async function doSearch(q) {
  if (!q.trim()) return;
  const res = $('#search-results');
  res.innerHTML = '<div class="spinner"></div>';
  const cat = $('#search-category')?.value || '';
  const catParam = cat ? `&category=${cat}` : '';

  // Build date params
  const { date_from, date_to } = getDateRange();
  let dateParams = '';
  if (date_from) dateParams += `&date_from=${date_from}`;
  if (date_to) dateParams += `&date_to=${date_to}`;

  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}${catParam}${dateParams}`);
    let results = data.results || [];
    // Filter low-relevance noise
    const filtered = results.filter(r => r.relevance_score >= 0.12);
    const dropped = results.length - filtered.length;
    results = filtered;

    // Build date label for result count
    let dateLabel = '';
    if (currentDateFilter === 'today') dateLabel = ' from today';
    else if (currentDateFilter === '7d') dateLabel = ' from last 7 days';
    else if (currentDateFilter === '30d') dateLabel = ' from last 30 days';
    else if (currentDateFilter === 'custom' && (date_from || date_to)) {
      dateLabel = ` from ${date_from || '...'}  →  ${date_to || '...'}`;
    }

    if (!results.length) {
      res.innerHTML = `<div class="empty-state"><div class="empty-icon">🤷</div><div class="empty-title">No results</div><div>Try a different query or time range.${dropped ? ` (${dropped} low-relevance results filtered)` : ''}</div></div>`;
      return;
    }
    const countMsg = `${results.length} results${dateLabel}${dropped ? ` <span style="opacity:0.5">(${dropped} low-relevance filtered)</span>` : ''}`;
    res.innerHTML = `<div style="color:var(--text-muted);font-size:0.85rem;margin-bottom:16px">${countMsg}</div>` +
      `<div class="timeline">${results.map((r, i) => {
        const time = formatTime(r.timestamp);
        const date = new Date(r.timestamp).toLocaleDateString();
        const rCat = r.category || 'other';
        const score = r.relevance_score !== undefined ? `<span class="relevance">${(r.relevance_score * 100).toFixed(0)}%</span>` : '';
        const badge = r.match_type === 'keyword' ? '<span class="match-badge keyword">🔤 Keyword</span>'
                    : r.match_type === 'meeting' ? '<span class="match-badge meeting">🎙️ Meeting</span>' : '';
        const summaryHl = highlightText(r.summary || '', q);
        const ocrSnippet = r.ocr_snippet ? `<div class="ocr-snippet">${highlightText(r.ocr_snippet, q)}</div>` : '';
        const detailSnippet = r.match_type === 'meeting' && r.details ? `<div class="ocr-snippet">${highlightText(r.details, q)}</div>` : '';
        // Use highlighted screenshot (shows purple boxes over matching OCR text on the image)
        const isMeeting = r.match_type === 'meeting';
        const hlUrl = !isMeeting && r.screenshot_url ? `${r.screenshot_url}/highlight?q=${encodeURIComponent(q)}` : '';
        const thumbUrl = hlUrl || r.screenshot_url;
        const meetingIcon = isMeeting ? '<div class="thumb" style="display:flex;align-items:center;justify-content:center;font-size:2rem;background:rgba(139,92,246,0.1)">🎙️</div>' : '';
        return `<div class="timeline-item search-result" style="animation-delay:${i * 0.06}s">
          ${isMeeting ? meetingIcon : thumbUrl ? `<img class="thumb" src="${thumbUrl}" loading="lazy" onclick="openModal('${thumbUrl}', ${r.id})" alt="">` : '<div class="thumb"></div>'}
          <div class="info">
            <div class="top"><span class="time">${date} ${time}</span><span class="app-name">${r.app_name || 'Unknown'}</span><span class="badge badge-${rCat}">${rCat}</span>${score} ${badge}</div>
            <div class="summary">${summaryHl}</div>
            ${ocrSnippet}
            ${detailSnippet}
          </div></div>`;
      }).join('')}</div>`;
    showToast(`🔍 ${results.length} results found`, 'info');
  } catch (err) { res.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div>${err.message}</div></div>`; }
}

// ══════════════════════════════════════════════════════════
