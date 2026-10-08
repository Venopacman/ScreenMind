async function renderSettings(el) {
  el.innerHTML = '<div class="spinner"></div>';
  let cfg, startupStatus;
  try {
    [cfg, startupStatus] = await Promise.all([
      api('/api/settings'),
      api('/api/startup/status').catch(() => ({ installed: false }))
    ]);
  } catch {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Cannot load settings</div></div>';
    return;
  }

  function _sec(icon, title) { return '<div class="settings-section"><span class="settings-section-icon">' + icon + '</span><span class="settings-section-title">' + title + '</span></div>'; }
  function _sw(id, checked) { return '<label class="toggle-switch"><input type="checkbox" id="' + id + '" ' + (checked ? 'checked' : '') + '><span class="toggle-slider"></span></label>'; }
  function _rp(name, val, label, cur) { return '<label class="radio-pill ' + (String(cur) === String(val) ? 'active' : '') + '"><input type="radio" name="' + name + '" value="' + val + '" ' + (String(cur) === String(val) ? 'checked' : '') + '> ' + label + '</label>'; }

  el.innerHTML = '<div class="settings-grid">'

  // ── CAPTURE ──
  + _sec('&#128248;', 'Capture')
  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Capture Interval</div><div class="settings-desc">How often to check for screen changes</div></div>'
  + '<span class="settings-value" id="interval-value">' + cfg.capture_interval + 's</span></div>'
  + '<input type="range" id="interval-slider" class="settings-slider" min="10" max="120" step="5" value="' + cfg.capture_interval + '"></div>'

  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Auto-Pause Heavy Apps</div><div class="settings-desc">Pause capture when games or video editors are active</div></div>'
  + _sw('auto-pause-toggle', cfg.auto_pause_heavy_apps) + '</div>'
  + '<div class="settings-input-row"><label class="settings-label">App keywords (comma-separated):</label>'
  + '<input type="text" id="heavy-apps-input" class="settings-text-input" value="' + (cfg.heavy_apps || '') + '" placeholder="game,valorant,blender,obs..."></div></div>'

  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Capture Active Monitor <span style="background:var(--accent-primary);color:#fff;font-size:10px;padding:2px 6px;border-radius:4px;margin-left:6px;vertical-align:middle">Beta</span></div><div class="settings-desc">Captures the screen with your active window instead of primary monitor. Recommended for multi-monitor setups. Works on Windows, Linux X11, and macOS.</div></div>'
  + _sw('capture-active-monitor', cfg.capture_active_monitor) + '</div></div>'

  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Start at System Startup</div><div class="settings-desc">Automatically launch ScreenMind in background when you log in</div></div>'
  + _sw('startup-toggle', startupStatus.installed) + '</div>'
  + '<div class="settings-note" id="startup-status"></div></div>'

  // ── MEETINGS ──
  + _sec('&#127908;', 'Meetings')
  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Call Tracking</div><div class="settings-desc">Record when calls start and end, with app, window title and room link. No audio is recorded.</div></div></div>'
  + '<div class="settings-input-row"><label class="settings-label">Call apps to detect:</label>'
  + '<input type="text" id="meeting-apps-input" class="settings-text-input" value="' + (cfg.meeting_apps || '') + '" placeholder="zoom,teams,meet,webex,slack..."></div></div>'

  // ── STORAGE ──
  + _sec('&#128451;', 'Storage')
  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Data Retention</div><div class="settings-desc">Auto-delete timeline data older than selected period</div></div></div>'
  + '<div class="settings-note">Data older than the selected period is permanently deleted on every startup.</div>'
  + '<div class="radio-group" id="retention-group">' + _rp('retention','1','1 Day',cfg.retention_days) + _rp('retention','7','7 Days',cfg.retention_days) + _rp('retention','30','30 Days',cfg.retention_days) + _rp('retention','90','90 Days',cfg.retention_days) + _rp('retention','0','Forever',cfg.retention_days) + '</div>'
  + '<div id="storage-estimate" class="settings-note" style="margin-top:8px;font-size:0.82rem"></div></div>'

  + renderExportCard()

  // ── PRIVACY & SECURITY ──
  + _sec('&#128737;', 'Privacy &amp; Security')
  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Sensitive Data Filter</div><div class="settings-desc">Auto-redact PII from captured text before storage</div></div>'
  + _sw('sensitive-filter-enabled', cfg.sensitive_filter_enabled) + '</div>'
  + '<div style="display:flex;flex-direction:column;gap:6px;margin-top:4px">'
  // Same order as the default in config.py, so saving the defaults stores nothing.
  + ['credit_card','ssn','api_key','jwt','password','email','phone','iban'].map(function(t) {
      var checked = (cfg.sensitive_filter_types || '').split(',').indexOf(t) >= 0 ? 'checked' : '';
      var labels = {credit_card:'Credit Cards', ssn:'SSN/ID Numbers', api_key:'API Keys', jwt:'JWT Tokens', password:'Passwords', email:'Email Addresses (domain kept)', phone:'Phone Numbers', iban:'IBANs'};
      return '<label style="display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--text-secondary);cursor:pointer"><input type="checkbox" class="filter-type-cb" value="' + t + '" ' + checked + ' style="accent-color:var(--accent)"> ' + labels[t] + '</label>';
    }).join('') + '</div></div>'

  + '<div class="settings-card"><div class="settings-card-header"><div><div class="settings-title">Screenshot Encryption</div><div class="settings-desc">Encrypt screenshots at rest (AES-128)</div></div>'
  + _sw('encryption-enabled', cfg.encryption_enabled) + '</div>'
  + '<div class="settings-note">Key stored in OS keyring. Uses the <code>cryptography</code> and <code>keyring</code> packages.</div></div>'

  + '<div class="settings-card" id="ui-events-card"><div class="settings-card-header"><div><div class="settings-title">UI Events <span style="background:var(--accent-primary);color:#fff;font-size:10px;padding:2px 6px;border-radius:4px;margin-left:6px;vertical-align:middle">Beta</span></div><div class="settings-desc">Record clicks, typed text, app switches and clipboard through OS accessibility APIs. Shows what you did between screenshots.</div></div>'
  + _sw('ui-events-enabled', cfg.ui_events_enabled) + '</div>'
  + '<div class="settings-note">Clicks, app switches, typed text and clipboard are on by default. Typed text and clipboard can hold private content: password fields are never recorded, and the Sensitive Data Filter runs on all text. Paused capture and blocked apps stop recording too. Works on macOS and Windows.</div>'
  + '<div style="display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px">'
  + [['click','Clicks'],['app_switch','App switches'],['window_focus','Window changes'],['text','Typed text'],['clipboard','Clipboard']].map(function(t) {
      var checked = (cfg.ui_events_types || '').split(',').indexOf(t[0]) >= 0 ? 'checked' : '';
      return '<label style="display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--text-secondary);cursor:pointer"><input type="checkbox" class="ui-event-type-cb" value="' + t[0] + '" ' + checked + ' style="accent-color:var(--accent)"> ' + t[1] + '</label>';
    }).join('') + '</div>'
  + '<div class="settings-toggle-row" style="margin-top:8px"><div><div class="settings-toggle-label">Capture on events</div><div class="settings-toggle-desc">Take a screenshot right after app switches, clicks, typing pauses and copies (at most one every 3s)</div></div>'
  + _sw('event-triggered-capture', cfg.event_triggered_capture) + '</div>'
  + '<div id="ui-events-status" class="settings-note" style="margin-top:8px"></div></div>'

  + '</div>';

  // Inject Save button into header bar
  var headerActions = document.getElementById('header-actions');
  if (headerActions) {
    headerActions.innerHTML = '<button class="btn btn-sm" id="save-settings" onclick="saveSettings()" style="padding:6px 18px;font-size:0.82rem;font-weight:600;border-radius:8px;background:rgba(255,255,255,0.06);color:var(--text-muted);border:1px solid rgba(255,255,255,0.08);cursor:default;transition:all 0.25s ease" disabled>Saved</button>';
  }

  // Radio button visual toggle
  el.querySelectorAll('.radio-pill input').forEach(function(radio) {
    radio.addEventListener('change', function() {
      var group = radio.closest('.radio-group');
      group.querySelectorAll('.radio-pill').forEach(function(p) { p.classList.remove('active'); });
      radio.closest('.radio-pill').classList.add('active');
    });
  });
  el.querySelectorAll('#retention-group input').forEach(function(radio) {
    radio.addEventListener('change', updateStorageEstimate);
  });
  var slider = document.getElementById('interval-slider');
  slider.addEventListener('input', function() {
    document.getElementById('interval-value').textContent = slider.value + 's';
  });

  // Track changes for save button state
  setTimeout(function() { _trackSettingsChanges(el); }, 100);

  // Startup toggle (uses separate API, not saveSettings)
  var startupToggle = document.getElementById('startup-toggle');
  if (startupToggle) {
    startupToggle.addEventListener('change', async function() {
      var statusEl = document.getElementById('startup-status');
      try {
        var endpoint = startupToggle.checked ? '/api/startup/install' : '/api/startup/uninstall';
        statusEl.textContent = 'Updating...';
        statusEl.style.color = 'var(--text-muted)';
        var res = await api(endpoint, { method: 'POST' });
        statusEl.textContent = res.message || (res.ok ? 'Done' : 'Failed');
        statusEl.style.color = res.ok ? '#10b981' : '#ef4444';
        setTimeout(function() { statusEl.textContent = ''; }, 3000);
      } catch (e) {
        statusEl.textContent = 'Error: ' + e.message;
        statusEl.style.color = '#ef4444';
        startupToggle.checked = !startupToggle.checked; // revert
      }
    });
  }

  updateStorageEstimate();
  initExportCard();
  loadUiEventsStatus();
}

async function loadUiEventsStatus() {
  var el = document.getElementById('ui-events-status');
  if (!el) return;
  var st;
  try { st = await api('/api/ui-events/status'); } catch { el.textContent = ''; return; }
  if (!st.supported) {
    el.innerHTML = 'Not supported on this platform yet.';
    return;
  }
  var p = st.permissions || {};
  function _perm(ok, label) {
    return '<span style="color:' + (ok ? '#10b981' : '#f59e0b') + '">' + (ok ? '&#10004; ' : '&#9888; ') + label + '</span>';
  }
  // Only macOS has permission grants; other backends need none.
  var isMac = st.backend === 'macos';
  var html = (isMac
      ? _perm(p.input_monitoring, 'Input Monitoring') + ' &nbsp; ' + _perm(p.accessibility, 'Accessibility')
      : _perm(true, 'No permissions needed'))
    + ' &nbsp; <span style="color:var(--text-muted)">' + (st.running ? 'Recording' : 'Stopped')
    + (st.running ? ' &middot; ' + st.events_recorded + ' events this session' : '')
    + (st.running && st.keys_tapped === false ? ' &middot; clicks only (no Input Monitoring)' : '') + '</span>';
  if (isMac && !p.all_granted) {
    html += '<div style="margin-top:6px">macOS asks for these for the app that started ScreenMind (Terminal, or Python for a login item), not for "ScreenMind". '
      + 'Grant both in System Settings &rarr; Privacy &amp; Security, then restart ScreenMind. '
      + '<button class="btn btn-sm" style="margin-left:4px" onclick="requestUiEventPermissions()">Ask macOS</button></div>';
  }
  if (st.last_error) {
    html += '<div style="margin-top:4px;color:#ef4444">Last error: ' + st.last_error.replace(/</g, '&lt;') + '</div>';
  }
  el.innerHTML = html;
}

window.requestUiEventPermissions = async function() {
  try {
    await api('/api/ui-events/permissions', { method: 'POST' });
  } catch {}
  setTimeout(loadUiEventsStatus, 1000);
};



async function updateStorageEstimate() {
  const el = document.getElementById('storage-estimate');
  if (!el) return;
  try {
    const data = await api('/api/storage-estimate');
    const selected = document.querySelector('input[name="retention"]:checked');
    const days = selected ? selected.value : '7';
    const est = data.estimates || {};
    const current = data.current_total_mb || 0;
    const perDay = data.avg_mb_per_day || 0;

    if (days === '0') {
      el.innerHTML = `📊 Current storage: <strong>${current} MB</strong> (${data.active_days} days tracked, ~${perDay} MB/day). <em>No auto-cleanup — storage will grow indefinitely.</em>`;
    } else {
      const estimated = est[days] || (perDay * parseInt(days));
      el.innerHTML = `📊 Current: <strong>${current} MB</strong> · Estimated for ${days} days: <strong>~${estimated} MB</strong> (~${perDay} MB/day)`;
    }
  } catch {
    el.textContent = '';
  }
}

// Hotkey capture function
window.saveSettings = async function() {
  var retention = document.querySelector('input[name="retention"]:checked');

  var body = {
    auto_pause_heavy_apps: document.getElementById('auto-pause-toggle').checked,
    heavy_apps: document.getElementById('heavy-apps-input').value,
    capture_interval: parseInt(document.getElementById('interval-slider').value),
    capture_active_monitor: document.getElementById('capture-active-monitor').checked,
    meeting_apps: document.getElementById('meeting-apps-input').value,
    retention_days: retention ? parseInt(retention.value) : 7,
    // Privacy
    sensitive_filter_enabled: document.getElementById('sensitive-filter-enabled').checked,
    sensitive_filter_types: (function() {
      var types = [];
      document.querySelectorAll('.filter-type-cb:checked').forEach(function(cb) { types.push(cb.value); });
      return types.join(',');
    })(),
    encryption_enabled: document.getElementById('encryption-enabled').checked,
    // UI events
    ui_events_enabled: document.getElementById('ui-events-enabled').checked,
    ui_events_types: (function() {
      var types = [];
      document.querySelectorAll('.ui-event-type-cb:checked').forEach(function(cb) { types.push(cb.value); });
      return types.join(',');
    })(),
    event_triggered_capture: document.getElementById('event-triggered-capture').checked,
  };
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    showToast('Settings saved', 'success');
    _markSettingsSaved();
    setTimeout(loadUiEventsStatus, 500);
  } catch {
    showToast('Failed to save settings', 'warning');
  }
};

// ── Settings Change Tracking ──────────────────────────────────────────

var _settingsSnapshot = '';

function _captureSettingsSnapshot(container) {
  // Capture current state of all form inputs as a string for comparison
  var parts = [];
  container.querySelectorAll('input, textarea, select').forEach(function(el) {
    if (el.hasAttribute('data-no-save')) return;  // e.g. the export card
    if (el.type === 'checkbox' || el.type === 'radio') {
      parts.push(el.id + ':' + el.checked);
    } else {
      parts.push(el.id + ':' + el.value);
    }
  });
  return parts.join('|');
}

function _getCurrentSettingsState(container) {
  return _captureSettingsSnapshot(container);
}

function _markSettingsUnsaved() {
  var btn = document.getElementById('save-settings');
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = 'Save';
  btn.style.background = '#10b981';
  btn.style.color = '#fff';
  btn.style.border = '1px solid #10b981';
  btn.style.cursor = 'pointer';
  btn.style.boxShadow = '0 0 12px rgba(16,185,129,0.3)';
}

function _markSettingsSaved() {
  var btn = document.getElementById('save-settings');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Saved';
  btn.style.background = 'rgba(255,255,255,0.06)';
  btn.style.color = 'var(--text-muted)';
  btn.style.border = '1px solid rgba(255,255,255,0.08)';
  btn.style.cursor = 'default';
  btn.style.boxShadow = 'none';
  // Update snapshot to current state
  var container = document.querySelector('.settings-grid');
  if (container) _settingsSnapshot = _captureSettingsSnapshot(container);
}

function _trackSettingsChanges(container) {
  // Capture initial snapshot
  _settingsSnapshot = _captureSettingsSnapshot(container);

  // Listen for any input change
  container.addEventListener('input', function() {
    var current = _getCurrentSettingsState(container);
    if (current !== _settingsSnapshot) {
      _markSettingsUnsaved();
    } else {
      _markSettingsSaved();
    }
  });
  container.addEventListener('change', function() {
    var current = _getCurrentSettingsState(container);
    if (current !== _settingsSnapshot) {
      _markSettingsUnsaved();
    } else {
      _markSettingsSaved();
    }
  });
}

// Clean up header-actions when navigating away from settings
document.addEventListener('click', function(e) {
  var navItem = e.target.closest('.nav-item');
  if (navItem && navItem.dataset.view !== 'settings') {
    var ha = document.getElementById('header-actions');
    if (ha) ha.innerHTML = '';
  }
});
window.addEventListener('hashchange', function() {
  var view = window.location.hash.slice(1);
  if (view && view !== 'settings') {
    var ha = document.getElementById('header-actions');
    if (ha) ha.innerHTML = '';
  }
});

// ── Init ──────────────────────────────────────────────────
function _initApp() {
  const initialView = window.location.hash.slice(1) || 'timeline';
  requestAnimationFrame(() => {
    const activeBtn = $(`[data-view="${initialView}"]`);
    if (activeBtn) moveIndicator(activeBtn);
  });
  navigate(initialView);
  pollStatus();
  _setPollInterval(15000);
}

window.shutdownScreenMind = async function() {
  if (!confirm('Stop ScreenMind?\n\nThe background process and API server will shut down.\nYou can restart it from the desktop shortcut or terminal.')) return;
  var btn = document.getElementById('shutdown-btn');
  btn.textContent = 'Stopping...';
  btn.disabled = true;
  try {
    await api('/api/shutdown', { method: 'POST' });
  } catch (e) {
    // Expected — server dies before response completes
  }
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text-muted);font-size:1.1rem">ScreenMind stopped.</div>';
};

// Show the welcome screen on first run; otherwise start the app
_checkAuth().then(function(firstRun) {
  if (!firstRun) {
    _initApp();
  }
});
