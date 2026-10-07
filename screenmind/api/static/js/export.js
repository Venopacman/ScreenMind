// ── Data export card (Settings → Storage) ─────────────────
// Downloads GET /api/export as a zip. Format: docs/export-format.md.
// Inputs carry data-no-save, so they don't count as unsaved settings.

var EXPORT_MAX_DAYS = 31;

function _exportToday() {
  var d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function renderExportCard() {
  var today = _exportToday();
  var dateStyle = 'color-scheme:dark;width:auto';
  return '<div class="settings-card" id="export-card"><div class="settings-card-header"><div><div class="settings-title">Data Export</div>'
    + '<div class="settings-desc">Download a zip of your activity for one or more days. One text file per work session, plus screenshots.</div></div></div>'
    + '<div class="settings-input-row"><label class="settings-label" for="export-user">User id or email (goes into the archive)</label>'
    + '<input type="text" id="export-user" data-no-save class="settings-text-input" placeholder="name@company.com" autocomplete="off"></div>'
    + '<div class="settings-input-row" style="display:flex;flex-wrap:wrap;gap:12px">'
    + '<div><label class="settings-label" for="export-from">From</label><input type="date" id="export-from" data-no-save class="settings-text-input" style="' + dateStyle + '" value="' + today + '" max="' + today + '"></div>'
    + '<div><label class="settings-label" for="export-to">To</label><input type="date" id="export-to" data-no-save class="settings-text-input" style="' + dateStyle + '" value="' + today + '" max="' + today + '"></div>'
    + '</div>'
    + '<label style="display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--text-secondary);cursor:pointer;margin-top:10px">'
    + '<input type="checkbox" id="export-screenshots" data-no-save checked style="accent-color:var(--accent)"> Include screenshots (large, and not redacted)</label>'
    + '<div id="export-preview" class="settings-note" style="margin-top:10px" aria-live="polite"></div>'
    + '<button class="btn btn-primary btn-sm" id="export-btn" type="button">Export</button>'
    + '</div>';
}

function _exportRange() {
  var from = document.getElementById('export-from').value;
  var to = document.getElementById('export-to').value;
  if (!from || !to) return { error: 'Pick both dates.' };
  if (to < from) return { error: '"To" is before "From".' };
  var days = Math.round((new Date(to) - new Date(from)) / 86400000) + 1;
  if (days > EXPORT_MAX_DAYS) return { error: 'Export at most ' + EXPORT_MAX_DAYS + ' days at a time.' };
  return { from: from, to: to, days: days };
}

var _exportPreviewSeq = 0;

async function _updateExportPreview() {
  var el = document.getElementById('export-preview');
  if (!el) return;
  var r = _exportRange();
  if (r.error) { el.textContent = r.error; return; }
  var seq = ++_exportPreviewSeq;
  el.textContent = 'Counting...';
  var p;
  try {
    p = await api('/api/export/preview?from=' + r.from + '&to=' + r.to);
  } catch (e) {
    if (seq === _exportPreviewSeq) el.textContent = 'Cannot count the data: ' + e.message;
    return;
  }
  if (seq !== _exportPreviewSeq) return;
  var t = { sessions: 0, activities: 0, meetings: 0, screenshots: 0, bytes: 0 };
  p.days.forEach(function(d) {
    t.sessions += d.sessions; t.activities += d.activities; t.meetings += d.meetings;
    t.screenshots += d.screenshots; t.bytes += d.screenshot_bytes;
  });
  if (!t.activities && !t.meetings) { el.textContent = 'No data for these dates.'; return; }
  var withShots = document.getElementById('export-screenshots').checked;
  function n(count, word) { return count + ' ' + word + (count === 1 ? '' : 's'); }
  el.textContent = n(r.days, 'day') + ': ' + n(t.sessions, 'session') + ', '
    + n(t.activities, 'screen') + ', ' + n(t.meetings, 'meeting') + '. '
    + (withShots ? 'Screenshots: ' + t.screenshots + ', about ' + _formatBytes(t.bytes) + '.' : 'Text only, small.');
}

function _startExport() {
  var user = document.getElementById('export-user').value.trim();
  if (!user) {
    showToast('Type a user id or email first.', 'warning');
    document.getElementById('export-user').focus();
    return;
  }
  var r = _exportRange();
  if (r.error) { showToast(r.error, 'warning'); return; }
  var withShots = document.getElementById('export-screenshots').checked;
  var url = '/api/export?user=' + encodeURIComponent(user) + '&from=' + r.from + '&to=' + r.to
    + '&screenshots=' + withShots;
  // A plain link lets the browser stream the zip to disk instead of holding it in memory.
  var a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  showToast(withShots ? 'Building the archive. With screenshots this can take a while.' : 'Building the archive.', 'info');
}

function initExportCard() {
  var card = document.getElementById('export-card');
  if (!card) return;
  ['export-from', 'export-to', 'export-screenshots'].forEach(function(id) {
    document.getElementById(id).addEventListener('change', _updateExportPreview);
  });
  document.getElementById('export-btn').addEventListener('click', _startExport);
  _updateExportPreview();
}
