//------------------------------------------------------------------
// Utility helpers
//------------------------------------------------------------------

function el(id) { return document.getElementById(id); }

function fmtBar(percent, width = 10) {
  const p = Math.max(0, Math.min(100, percent | 0));
  const filled = Math.round(p / (100 / width));
  return '[' + '█'.repeat(filled) + '░'.repeat(width - filled) + ']';
}

function setText(id, text) {
  const e = el(id);
  if (e) e.textContent = text;
}

function setHtml(id, html) {
  const e = el(id);
  if (e) e.innerHTML = html;
}

function pad2(n) { return String(n).padStart(2, '0'); }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function timeAgo(iso) {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}

//------------------------------------------------------------------
// Clock
//------------------------------------------------------------------

const DAYS = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
const MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];

function renderClock() {
  const d = new Date();
  const t = pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  const date = DAYS[d.getDay()] + ' ' + pad2(d.getDate()) + '.' + MONTHS[d.getMonth()] + '.' + d.getFullYear();
  setText('clock', t + ' · ' + date);
}

//------------------------------------------------------------------
// Renderers per provider
//------------------------------------------------------------------

function metaTag(provider) {
  if (!provider) return { text: '—', mocked: false };
  if (provider.status === 'mocked') return { text: 'MOCK', mocked: true };
  if (provider.status === 'unconfigured') return { text: 'OFFLINE', mocked: false };
  if (provider.status === 'error') return { text: 'ERR', mocked: false };
  if (provider.updated_at) return { text: timeAgo(provider.updated_at) + ' ago', mocked: false };
  return { text: '—', mocked: false };
}

function setMeta(id, provider) {
  const m = metaTag(provider);
  const e = el(id);
  if (!e) return;
  e.textContent = m.text;
  e.classList.toggle('mocked', m.mocked);
}

function renderServices(p) {
  setMeta('services-meta', p);
  if (!p || p.status === 'pending') { setHtml('services-body', '<div class="dim">loading…</div>'); return; }
  const items = p.items || {};
  const order = ['anthropic', 'openai', 'github', 'linear', 'vercel'];
  const labelFor = { anthropic: 'ANTHROPIC API', openai: 'OPENAI API', github: 'GITHUB', linear: 'LINEAR', vercel: 'VERCEL' };
  let okCount = 0;
  const rows = order.map(name => {
    const it = items[name] || { indicator: 'unknown' };
    const ind = it.indicator;
    let dotCls = 'unknown', stateClass = 'dim', text = 'UNK';
    if (ind === 'none')                        { dotCls = 'ok';   stateClass = 'ok';   text = 'OK'; okCount++; }
    else if (ind === 'minor')                  { dotCls = 'warn'; stateClass = 'warn'; text = 'MINOR'; }
    else if (ind === 'major' || ind === 'critical') { dotCls = 'alert'; stateClass = 'alert'; text = ind.toUpperCase(); }
    return `<div class="row">
      <span><span class="dot ${dotCls}"></span>${labelFor[name]}</span>
      <span class="${stateClass}">${text}</span>
    </div>`;
  }).join('');
  el('services-meta').textContent = `${okCount}/${order.length} OK`;
  setHtml('services-body', rows);
}

function renderClaude(p) {
  setMeta('claude-meta', p);
  if (!p || p.status === 'pending') { setHtml('claude-body', '<div class="dim">loading…</div>'); return; }
  const five = p.five_hour || {};
  const week = p.weekly || {};
  const fivePct = five.percent || 0;
  const weekPct = week.percent || 0;
  const fiveCls = fivePct >= 80 ? 'warn' : 'primary';
  const weekCls = weekPct >= 80 ? 'warn' : 'primary';
  setHtml('claude-body', `
    <div class="row"><span class="dim">5-HR WINDOW</span><span>resets ${escapeHtml(five.resets_at || '—')}</span></div>
    <div class="bar"><span class="btrack ${fiveCls}">${fmtBar(fivePct, 12)}</span><span class="bval ${fiveCls}">${fivePct}%</span></div>
    <div class="row"><span class="dim">WEEKLY</span><span>resets ${escapeHtml(week.resets_at || '—')}</span></div>
    <div class="bar"><span class="btrack ${weekCls}">${fmtBar(weekPct, 12)}</span><span class="bval ${weekCls}">${weekPct}%</span></div>
  `);
}

function renderSessions(p) {
  setMeta('sessions-meta', p);
  if (!p || p.status === 'pending') { setHtml('sessions-body', '<div class="dim">loading…</div>'); return; }
  const s = p.cc_sessions || { live: 0, idle: 0, total_today: 0 };
  setHtml('sessions-body', `
    <div class="row"><span class="dim">LIVE</span><span class="accent blink-cursor">${s.live}</span></div>
    <div class="row"><span class="dim">IDLE</span><span>${s.idle}</span></div>
    <div class="row"><span class="dim">TOTAL TODAY</span><span>${s.total_today ?? '—'}</span></div>
    <div class="panel-foot">scan ~/.claude/projects</div>
  `);
}

function localYMD(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function calDayLabel(dateStr, todayStr, tomorrowStr) {
  if (dateStr === todayStr) return 'TODAY';
  if (dateStr === tomorrowStr) return 'TOMORROW';
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  const dow = ['SUN','MON','TUE','WED','THU','FRI','SAT'][dt.getDay()];
  const mon = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][dt.getMonth()];
  return `${dow} · ${mon} ${d}`;
}
function renderCalendar(p) {
  setMeta('cal-meta', p);
  if (!p || p.status === 'pending') { setHtml('cal-body', '<div class="dim">loading…</div>'); return; }
  const events = p.events || [];
  if (!events.length) { setHtml('cal-body', '<div class="dim">no events today</div>'); return; }
  el('cal-meta').textContent = events.length + ' EVENTS' + (p.status === 'mocked' ? ' · MOCK' : '');
  const now = new Date();
  const todayStr = localYMD(now);
  const tom = new Date(now); tom.setDate(tom.getDate() + 1);
  const tomorrowStr = localYMD(tom);
  const parts = [];
  let prevDate = null;
  for (const ev of events) {
    if (ev.start_date && ev.start_date !== prevDate) {
      // Suppress the leading "TODAY" header — the panel context implies it.
      // Always emit a separator on day changes, and emit one before the first
      // event when nothing is scheduled today (so the user sees the date jump).
      if (!(prevDate === null && ev.start_date === todayStr)) {
        parts.push(`<div class="cal-day-sep">${escapeHtml(calDayLabel(ev.start_date, todayStr, tomorrowStr))}</div>`);
      }
      prevDate = ev.start_date;
    }
    let cls = '';
    if (ev.is_now) cls = 'now';
    else if (ev.is_next) cls = 'next';
    const marker = ev.is_now ? '▸ NOW' : (ev.is_next ? '  NEXT' : '     ');
    parts.push(`<div class="cal-row ${cls}">
      <span class="when">${escapeHtml(marker)}  ${escapeHtml(ev.start)}</span>
      <span class="what">${escapeHtml(ev.title)}</span>
    </div>`);
  }
  setHtml('cal-body', parts.join(''));
}

function renderTasks(p) {
  setMeta('tasks-meta', p);
  if (!p || p.status === 'pending') { setHtml('tasks-body', '<div class="dim">loading…</div>'); return; }
  const items = p.items || [];
  el('tasks-meta').textContent = (p.open_count ?? items.length) + ' OPEN' + (p.status === 'mocked' ? ' · MOCK' : '');
  const rows = items.map(t => {
    const pri = t.priority === 'high' ? '!' : ' ';
    const dueCls = t.due === 'TODAY' ? 'warn' : (/\d+D$/.test(t.due || '') ? 'alert' : 'dim');
    const urlAttr = t.url ? ` data-url="${escapeHtml(t.url)}"` : '';
    return `<div class="task"${urlAttr}>
      <span class="pri">${pri}</span>
      <span class="src">${escapeHtml(t.source || '')}</span>
      <span class="title">${escapeHtml(t.title || '')}</span>
      <span class="due ${dueCls}">${escapeHtml(t.due || '')}</span>
    </div>`;
  }).join('');
  setHtml('tasks-body', rows);
}

function _safeId(s) {
  return String(s).replace(/[^A-Za-z0-9_-]/g, '_');
}

function _ensureLinearPanel(label) {
  const safe = _safeId(label);
  const panelId = 'panel-linear-' + safe;
  if (el(panelId)) return safe;

  const panel = document.createElement('div');
  panel.className = 'panel grow';
  panel.id = panelId;

  const title = document.createElement('div');
  title.className = 'panel-title';
  const ttl = document.createElement('span');
  ttl.className = 'ttl';
  ttl.textContent = 'LINEAR · ' + label;
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.id = 'linear-meta-' + safe;
  meta.textContent = '—';
  title.appendChild(ttl);
  title.appendChild(meta);

  const body = document.createElement('div');
  body.id = 'linear-body-' + safe;
  body.className = 'dim';
  body.textContent = 'Loading…';

  panel.appendChild(title);
  panel.appendChild(body);
  el('linear-panels').appendChild(panel);
  return safe;
}

function _ensureUnconfiguredPanel(message) {
  const container = el('linear-panels');
  Array.from(container.children).forEach(child => {
    if (child.id && child.id.startsWith('panel-linear-') && child.id !== 'panel-linear-unconfigured') {
      child.remove();
    }
  });
  let panel = el('panel-linear-unconfigured');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'panel grow';
    panel.id = 'panel-linear-unconfigured';
    const title = document.createElement('div');
    title.className = 'panel-title';
    const ttl = document.createElement('span');
    ttl.className = 'ttl';
    ttl.textContent = 'LINEAR';
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = 'OFFLINE';
    title.appendChild(ttl);
    title.appendChild(meta);
    const body = document.createElement('div');
    body.id = 'linear-unconfigured-body';
    body.className = 'dim';
    panel.appendChild(title);
    panel.appendChild(body);
    container.appendChild(panel);
  }
  el('linear-unconfigured-body').textContent = message || 'unconfigured';
}

function renderLinear(state) {
  const container = el('linear-panels');
  if (!container) return;

  if (state && state.status === 'unconfigured') {
    _ensureUnconfiguredPanel(state.message || '');
    return;
  }

  if (!state || typeof state !== 'object') {
    Array.from(container.children).forEach(c => c.remove());
    return;
  }

  const labels = Object.keys(state).filter(k => k !== 'status' && k !== 'message');
  const wantedIds = new Set(labels.map(l => 'panel-linear-' + _safeId(l)));

  const placeholder = el('panel-linear-unconfigured');
  if (placeholder) placeholder.remove();

  Array.from(container.children).forEach(child => {
    if (child.id && child.id.startsWith('panel-linear-') && !wantedIds.has(child.id)) {
      child.remove();
    }
  });

  labels.forEach(label => {
    const safe = _ensureLinearPanel(label);
    renderLinearPanel(label, safe, state[label]);
  });
}

function renderLinearPanel(label, safe, p) {
  const metaId = 'linear-meta-' + safe;
  const bodyId = 'linear-body-' + safe;
  const meta = el(metaId);
  const body = el(bodyId);
  if (!meta || !body) return;

  setMeta(metaId, p);

  if (!p || p.status === 'pending') {
    setHtml(bodyId, '<div class="dim">loading…</div>');
    return;
  }
  if (p.status === 'error') {
    setHtml(metaId, '<span class="err">ERR</span> <span class="dim">' + escapeHtml((p.error||'').slice(0,80)) + '</span>');
    if (body.textContent.trim() === '' || body.textContent.toLowerCase().includes('loading')) {
      setHtml(bodyId, '<div class="err">' + escapeHtml(p.error || 'error') + '</div>');
    }
    return;
  }

  const issues = p.issues || { open_count: 0, items: [] };
  const cycle = p.cycle || { present: false };

  meta.textContent = (issues.open_count ?? 0) + ' OPEN';

  let html = '';

  if (cycle.present) {
    const days = cycle.ends_in_days;
    let endsTxt, endsCls;
    if (days < 0)       { endsTxt = 'PAST DUE'; endsCls = 'alert'; }
    else if (days <= 1) { endsTxt = 'ENDS ' + (days === 0 ? 'TODAY' : 'TMRW'); endsCls = 'warn'; }
    else                { endsTxt = 'ENDS ' + days + 'D'; endsCls = ''; }
    const bar = fmtBar(cycle.progress_pct || 0, 12);
    const multi = cycle.multi_team ? ' <span class="dim">+MORE</span>' : '';
    html += '<div class="cycle-strip">'
      + '<span class="dim">CYCLE ' + escapeHtml(String(cycle.number ?? '')) + '</span>'
      + '<span class="' + endsCls + '">' + escapeHtml(endsTxt) + '</span>'
      + '<span class="bar">' + escapeHtml(bar) + '</span>'
      + '<span class="dim">' + (cycle.progress_pct || 0) + '%</span>'
      + '<span class="dim">' + (cycle.completed || 0) + '/' + (cycle.total || 0) + '</span>'
      + multi
      + '</div>';
  }

  const items = issues.items || [];
  if (items.length === 0) {
    html += '<div class="dim">no open issues</div>';
  } else {
    html += items.map(it => {
      const pri = it.priority === 'high' ? '!' : ' ';
      const dueCls = it.due === 'TODAY' ? 'warn'
                    : (it.due === 'OVERDUE' ? 'alert'
                    : (/\d+D$/.test(it.due || '') ? 'alert' : 'dim'));
      const ident = it.identifier ? '<span class="dim">' + escapeHtml(it.identifier) + '</span> ' : '';
      const urlAttr = it.url ? ' data-url="' + escapeHtml(it.url) + '"' : '';
      return '<div class="task"' + urlAttr + '>'
        + '<span class="pri">' + pri + '</span>'
        + '<span class="src">' + escapeHtml(it.team || '') + '</span>'
        + '<span class="title">' + ident + escapeHtml(it.title || '') + '</span>'
        + '<span class="due ' + dueCls + '">' + escapeHtml(it.due || '') + '</span>'
        + '</div>';
    }).join('');
  }

  setHtml(bodyId, html);
}

let githubTab = 'overview';
let githubLast = null;

function ghPrList(items, emptyMsg) {
  if (!items || !items.length) return `<div class="dim" style="margin-top:var(--space-3)">${emptyMsg}</div>`;
  return items.map(pr => {
    const urlAttr = pr.url ? ` data-url="${escapeHtml(pr.url)}"` : '';
    return `
    <div class="pr-item"${urlAttr}>
      <span class="pr-title">#${pr.number} ${escapeHtml(pr.title)}${pr.draft ? ' <span class="dim" style="font-size:var(--fs-xs)">[draft]</span>' : ''}</span>
      <span class="pr-repo">${escapeHtml(pr.repo || '')}</span>
    </div>
  `;
  }).join('');
}

function ghIssueList(items, emptyMsg) {
  if (!items || !items.length) return `<div class="dim" style="margin-top:var(--space-3)">${emptyMsg}</div>`;
  return items.map(i => {
    const urlAttr = i.url ? ` data-url="${escapeHtml(i.url)}"` : '';
    return `
    <div class="pr-item"${urlAttr}>
      <span class="pr-title">#${i.number} ${escapeHtml(i.title)}</span>
      <span class="pr-repo">${escapeHtml(i.repo || '')}</span>
    </div>
  `;
  }).join('');
}

const GH_KIND_LABEL = {
  push: 'PSH', pr: 'PR', review: 'REV', issue: 'ISS', comment: 'CMT',
  create: 'NEW', delete: 'DEL', fork: 'FRK', star: 'STR',
  release: 'REL', public: 'PUB', discussion: 'DSC',
};

function ghActivityList(items) {
  if (!items || !items.length) return '<div class="dim" style="margin-top:var(--space-3)">no recent activity</div>';
  return items.map(ev => {
    const kind = ev.kind || '?';
    const cls = ev.cls || kind;
    const label = GH_KIND_LABEL[kind] || kind.toUpperCase().slice(0, 3);
    const urlAttr = ev.url ? ` data-url="${escapeHtml(ev.url)}"` : '';
    return `
    <div class="activity-item"${urlAttr}>
      <span class="activity-kind activity-${escapeHtml(cls)}">${escapeHtml(label)}</span>
      <span class="activity-detail">${escapeHtml(ev.detail || '')}</span>
      <span class="activity-meta">${escapeHtml(ev.repo || '')} · ${timeAgo(ev.at)}</span>
    </div>`;
  }).join('');
}

function renderGithubView(p) {
  const rr = p.review_requested || { count: 0, items: [] };
  const my = p.my_open_prs || { count: 0, items: [] };
  const ia = p.issues_assigned || { count: 0, items: [] };
  const ev = p.recent_events || { items: [] };
  const today = p.commits_today ?? 0;

  if (githubTab === 'prs') {
    return `
      <div class="row"><span class="dim">Awaiting my review</span><span class="${rr.count > 0 ? 'warn' : 'ok'}">${rr.count}</span></div>
      ${ghPrList(rr.items, 'queue is clear')}
      <div class="row" style="margin-top:var(--space-4)"><span class="dim">My open PRs</span><span>${my.count}</span></div>
      ${ghPrList(my.items, 'no open PRs')}
    `;
  }
  if (githubTab === 'issues') {
    return `
      <div class="row"><span class="dim">Assigned to me</span><span class="${ia.count > 0 ? 'warn' : 'ok'}">${ia.count}</span></div>
      ${ghIssueList(ia.items, 'no assigned issues')}
    `;
  }
  if (githubTab === 'activity') {
    return ghActivityList(ev.items);
  }
  // overview
  let prList = '';
  if (rr.items && rr.items.length) {
    prList = '<div class="dim" style="margin-top:var(--space-3);font-size:var(--fs-sm)">REVIEW REQUESTED</div>' + ghPrList(rr.items, '');
  }
  return `
    <div class="row"><span class="dim">PRs awaiting review</span><span class="${rr.count > 0 ? 'warn' : 'ok'}">${rr.count}</span></div>
    <div class="row"><span class="dim">PRs mine open</span><span>${my.count}</span></div>
    <div class="row"><span class="dim">Issues assigned</span><span>${ia.count}</span></div>
    <div class="row"><span class="dim">Commits today</span><span class="ok">${today}</span></div>
    <div class="gh-overview-extra">${prList}</div>
  `;
}

function renderGithub(p) {
  githubLast = p;
  setMeta('github-meta', p);
  if (!p) { setHtml('github-body', '<div class="dim">loading…</div>'); return; }
  if (p.status === 'unconfigured') {
    setHtml('github-body', `<div class="err">unconfigured</div><div class="dim">${escapeHtml(p.message || '')}</div>`);
    return;
  }
  if (p.status === 'error') {
    setHtml('github-body', `<div class="err">${escapeHtml(p.error || 'error')}</div>`);
    return;
  }
  el('github-meta').textContent = '@' + (p.username || '—');
  setHtml('github-body', renderGithubView(p));
}

// Wire tab clicks once on load.
(function initGithubTabs() {
  const tabs = document.getElementById('github-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (e) => {
    const t = e.target.closest('.tab');
    if (!t || t.classList.contains('dead')) return;
    const name = t.dataset.tab;
    if (!name || name === githubTab) return;
    githubTab = name;
    tabs.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
    if (githubLast) renderGithub(githubLast);
  });
})();

function renderHeatmap(p) {
  setMeta('heatmap-meta', p);
  if (!p || !p.heatmap) {
    if (p && p.status === 'unconfigured') {
      setHtml('heatmap-body', '<div class="dim">unconfigured</div>');
    } else {
      setHtml('heatmap-body', '<div class="dim">loading…</div>');
    }
    return;
  }
  const days = (p.heatmap.recent_days || []).slice(-140); // 20 weeks
  const todayIso = new Date().toISOString().slice(0, 10);
  const maxCount = Math.max(1, ...days.map(d => d.count));
  // group into 7-day columns to look like a calendar grid (rotated)
  // 20 columns (weeks) x 7 rows (days)
  const grid = [];
  for (let w = 0; w < 20; w++) {
    for (let r = 0; r < 7; r++) {
      const idx = w * 7 + r;
      const d = days[idx];
      if (!d) { grid.push('<span class="day l0"></span>'); continue; }
      const lvl = Math.min(5, Math.ceil((d.count / maxCount) * 5));
      const isToday = d.date === todayIso ? '1' : '0';
      grid.push(`<span class="day l${lvl}" data-today="${isToday}" title="${d.date}: ${d.count}"></span>`);
    }
  }
  setHtml('heatmap-body', `
    <div class="row"><span class="dim">Total (year)</span><span class="ok">${p.heatmap.total_year}</span></div>
    <div class="heatmap">${grid.join('')}</div>
    <div class="heatmap-legend">
      less
      <span class="sq l1"></span>
      <span class="sq l2"></span>
      <span class="sq l3"></span>
      <span class="sq l4"></span>
      <span class="sq l5"></span>
      more
    </div>
  `);
}

function tracePath(hist, key, W, H, maxVal) {
  const n = hist.length;
  if (n < 2) return '';
  const pad = Math.min(2, H * 0.1);
  return hist.map((s, i) => {
    const x = (i / (n - 1)) * W;
    const y = H - (s[key] / maxVal) * (H - pad * 2) - pad;
    return (i === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1);
  }).join(' ');
}

function updateNetTraces(hist) {
  // Updates both the right-rail oscilloscope (240x80) and the header sparkline (60x12).
  const targets = [
    ['net-trace-up',   'up',   240, 80],
    ['net-trace-down', 'down', 240, 80],
    ['spark-up',       'up',   60,  12],
    ['spark-down',     'down', 60,  12],
  ];
  if (hist.length < 2) {
    targets.forEach(([id]) => { const e = el(id); if (e) e.setAttribute('d', ''); });
    const peakEl = el('trace-peak');
    if (peakEl) peakEl.textContent = 'peak --.-';
    return;
  }
  const maxVal = Math.max(0.1, ...hist.flatMap(s => [s.up, s.down]));
  targets.forEach(([id, key, W, H]) => {
    const e = el(id);
    if (e) e.setAttribute('d', tracePath(hist, key, W, H, maxVal));
  });
  const peakEl = el('trace-peak');
  if (peakEl) peakEl.textContent = 'peak ' + maxVal.toFixed(1);
}

function renderRail(sys) {
  // Right rail + header sparkline — both fed from sys.net_history.
  if (!sys || sys.status === 'pending') return;

  const upEl = el('net-up');
  const downEl = el('net-down');
  if (upEl) upEl.textContent = (sys.net_up_mbps ?? 0).toFixed(1);
  if (downEl) downEl.textContent = (sys.net_down_mbps ?? 0).toFixed(1);

  updateNetTraces(sys.net_history || []);

  const bus = {
    cpu:    (sys.cpu_percent ?? 0) + '%',
    mem:    (sys.mem_percent ?? 0) + '%',
    disk:   (sys.disk_percent ?? 0) + '%',
    host:   (sys.host || '—').toUpperCase(),
    filter: sys.status === 'ok' ? 'FULL' : (sys.status || '—').toUpperCase(),
    drift:  shortenUptime(sys.uptime),
  };
  document.querySelectorAll('.rail-right [data-bus]').forEach(row => {
    const v = row.querySelector('.v');
    if (v) v.textContent = bus[row.dataset.bus] ?? '—';
  });
}

// Map service-status indicator -> {3-letter code, state class}.
const SERVICE_STATE = {
  none:     { code: 'OK',  cls: 'state-ok' },
  minor:    { code: 'MIN', cls: 'state-warn' },
  major:    { code: 'MAJ', cls: 'state-alert' },
  critical: { code: 'CRIT', cls: 'state-alert' },
  unknown:  { code: 'UNK', cls: 'state-dim' },
};

function setRailValue(selector, text, stateCls) {
  const el = document.querySelector(selector + ' .v');
  if (!el) return;
  el.textContent = text;
  if (stateCls) {
    el.classList.remove('state-ok', 'state-warn', 'state-alert', 'state-dim');
    el.classList.add(stateCls);
  }
}

function renderLeftRail(providers) {
  // Bind a handful of left-rail TRACE FREQ rows to real provider state.
  // The rest stay decorative — pure FUI flourish.
  const sys = providers.system;
  if (sys && sys.status === 'ok') {
    const cpu = sys.cpu_percent ?? 0;
    setRailValue('.rail-left [data-real="cpu"]', '+' + cpu.toFixed(1), 'state-warn');
  }
  const cl = providers.claude;
  if (cl && cl.cc_sessions) {
    setRailValue('.rail-left [data-real="cc-live"]', String(cl.cc_sessions.live ?? 0), 'state-ok');
  }
  const svc = providers.services;
  if (svc && svc.items) {
    for (const [name, info] of Object.entries(svc.items)) {
      const state = SERVICE_STATE[info.indicator] || SERVICE_STATE.unknown;
      setRailValue(`.rail-left [data-service="${name}"]`, state.code, state.cls);
    }
  }
}

function shortenUptime(s) {
  if (!s) return '—';
  const m = s.match(/(\d+)d\s+(\d+)h/);
  return m ? `${m[1]}D ${m[2]}H` : s.toUpperCase();
}

function renderSystem(sys) {
  if (!sys || sys.status === 'pending') { setText('syschip', 'CPU --% · MEM --% · UP --'); return; }
  const cpu = sys.cpu_percent ?? 0;
  const mem = sys.mem_percent ?? 0;
  const up = sys.uptime || '—';
  setText('syschip', `CPU ${cpu}% · MEM ${mem}% · UP ${up}`);
}

function renderTicker(events) {
  if (!events || !events.length) {
    setHtml('ticker-track', '<span class="dim">awaiting events…</span>');
    return;
  }
  // Duplicate the list so the CSS scroll never has empty gaps
  const items = events.slice(0, 30).reverse().map(ev => `
    <span class="ticker-item ${escapeHtml(ev.level || 'info')}">
      <span class="ts">[${escapeHtml((ev.ts || '').slice(11, 19))}]</span>
      <span class="src">${escapeHtml(ev.source || '')}</span>
      ${escapeHtml(ev.text || '')}
    </span>
  `).join('');
  setHtml('ticker-track', items + items);
}

//------------------------------------------------------------------
// Polling loop
//------------------------------------------------------------------

let lastTickerLen = -1;

//------------------------------------------------------------------
// Network panel — WAN ip / ISP / region / VPN
//------------------------------------------------------------------

function renderWeather(p) {
  const w = el('weather');
  if (!w) return;
  if (!p || p.status === 'pending' || p.status === 'unconfigured' || p.status === 'error'
      || typeof p.temp !== 'number') {
    w.textContent = '—';
    w.title = (p && p.error) || (p && p.message) || '';
    return;
  }
  const t = Math.round(p.temp);
  const unit = p.unit || 'F';
  const loc = (p.location || '').toUpperCase();
  w.textContent = loc ? `${t}°${unit} · ${loc}` : `${t}°${unit}`;
  // Hover reveals the full WMO condition (CLEAR, T-STORM, etc).
  w.title = p.condition
    ? (loc ? `${p.condition} · ${loc}` : p.condition)
    : loc;
}

function renderNetwork(p) {
  setMeta('network-meta', p);
  if (!p || p.status === 'pending') { setHtml('network-body', '<div class="dim">loading…</div>'); return; }
  if (p.status === 'error' && !p.wan_ip) {
    // ipinfo failed and we have no cached value — still show VPN state if any
    const vpnTxt = p.vpn_active ? 'ON' : 'OFF';
    const vpnCls = p.vpn_active ? 'warn' : 'dim';
    setHtml('network-body',
      '<div class="err">' + escapeHtml(p.error || 'error') + '</div>' +
      '<div class="net-row"><span class="k">VPN</span><span class="v ' + vpnCls + '">' + vpnTxt + '</span></div>'
    );
    return;
  }
  const vpnActive = !!p.vpn_active;
  const vpnLine = vpnActive
    ? (p.vpn_ifaces || []).map(i => escapeHtml(i.iface) + ' ' + escapeHtml(i.ip)).join(', ') || 'ON'
    : 'OFF';
  const rows = [
    ['WAN',    escapeHtml(p.wan_ip || '—'), 'ok'],
    ['ISP',    escapeHtml((p.isp || '').trim() || '—'), ''],
    ['REGION', escapeHtml(p.region || '—'), ''],
    ['ASN',    escapeHtml(p.asn || '—'), ''],
    ['VPN',    vpnLine, vpnActive ? 'warn' : 'dim'],
  ];
  setHtml('network-body', rows.map(([k, v, cls]) =>
    '<div class="net-row"><span class="k">' + k + '</span><span class="v ' + (cls || '') + '">' + v + '</span></div>'
  ).join(''));
}

//------------------------------------------------------------------
// Top processes panel — tabbed CPU/MEM
//------------------------------------------------------------------

let procsTab = 'cpu';

function renderProcs(sys) {
  setMeta('procs-meta', sys);
  if (!sys || sys.status === 'pending') { setHtml('procs-body', '<div class="dim">loading…</div>'); return; }
  const list = procsTab === 'cpu' ? (sys.top_cpu || []) : (sys.top_mem || []);
  if (!list.length) { setHtml('procs-body', '<div class="dim">no data</div>'); return; }
  const unit = procsTab === 'cpu' ? '%' : '%';
  const valKey = procsTab === 'cpu' ? 'cpu' : 'mem';
  setHtml('procs-body', list.map(r =>
    '<div class="proc-row">' +
      '<span class="name">' + escapeHtml(r.name || '?') + '</span>' +
      '<span class="pid">' + escapeHtml(String(r.pid ?? '')) + '</span>' +
      '<span class="val">' + (r[valKey] ?? 0).toFixed(1) + unit + '</span>' +
    '</div>'
  ).join(''));
}

document.addEventListener('click', e => {
  const t = e.target.closest('[data-procs-tab]');
  if (!t) return;
  procsTab = t.dataset.procsTab;
  document.querySelectorAll('#procs-toggle .t').forEach(x =>
    x.classList.toggle('active', x.dataset.procsTab === procsTab)
  );
  // Re-render immediately from the last-seen system blob — no need to wait
  // for the next 5s poll.
  if (lastSystem) renderProcs(lastSystem);
});

let lastSystem = null;

//------------------------------------------------------------------
// Scratchpad — debounced autosave to ~/.ds-dash/scratchpad.txt
//------------------------------------------------------------------

let scratchSaveTimer = null;
let scratchLoaded = false;

async function loadScratchpad() {
  try {
    const r = await fetch('/api/scratchpad', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    const ta = el('scratch-body');
    if (ta && !scratchLoaded) {
      // Only seed the textarea if the user hasn't started typing yet — avoids
      // clobbering a race where the user fires off a character before the GET
      // returns.
      if (!ta.value) ta.value = j.content || '';
      scratchLoaded = true;
      setText('scratch-meta', (ta.value.length || 0) + ' chars');
    }
  } catch (e) {
    setText('scratch-meta', 'load err');
    console.warn('scratchpad load failed:', e);
  }
}

async function saveScratchpad() {
  const ta = el('scratch-body');
  if (!ta) return;
  // POSTs to a localhost endpoint are cheap; if two race the latest one wins,
  // which is what the user wants.
  setText('scratch-meta', 'saving…');
  try {
    const r = await fetch('/api/scratchpad', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: ta.value }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    setText('scratch-meta', ta.value.length + ' chars');
  } catch (e) {
    setText('scratch-meta', 'save err');
    console.warn('scratchpad save failed:', e);
  }
}

(function wireScratchpad() {
  const ta = el('scratch-body');
  if (!ta) return;
  ta.addEventListener('input', () => {
    if (!scratchLoaded) return;  // don't write back the placeholder before load completes
    if (scratchSaveTimer) clearTimeout(scratchSaveTimer);
    setText('scratch-meta', 'editing…');
    scratchSaveTimer = setTimeout(saveScratchpad, 600);
  });
})();

//------------------------------------------------------------------
// Click-to-open for any row carrying data-url
//------------------------------------------------------------------

document.addEventListener('click', e => {
  const row = e.target.closest('[data-url]');
  if (!row) return;
  const url = row.dataset.url;
  if (!url) return;
  // _blank lets macOS Universal Links route to the desktop app for Linear /
  // Motion if installed, and otherwise opens a new browser tab.
  window.open(url, '_blank', 'noopener,noreferrer');
});

async function poll() {
  try {
    const r = await fetch('/api/state.json', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const s = await r.json();
    document.body.classList.remove('offline');
    el('connection').textContent = 'ON STATION';

    const op = (s.meta && s.meta.operator) || 'OPERATOR';
    if (el('opname').textContent !== op) el('opname').textContent = op;

    const p = s.providers || {};
    renderServices(p.services);
    renderNetwork(p.network);
    renderWeather(p.weather);
    renderClaude(p.claude);
    renderSessions(p.claude);
    renderCalendar(p.calendar);
    renderTasks(p.tasks);
    renderLinear(p.linear);
    renderGithub(p.github);
    renderHeatmap(p.github);
    renderSystem(p.system);
    lastSystem = p.system;
    renderProcs(p.system);
    renderRail(p.system);
    renderLeftRail(p);

    if ((s.ticker || []).length !== lastTickerLen) {
      renderTicker(s.ticker || []);
      lastTickerLen = (s.ticker || []).length;
    }
  } catch (e) {
    document.body.classList.add('offline');
    el('connection').textContent = 'OFFLINE';
    console.warn('poll failed:', e);
  }
}

//------------------------------------------------------------------
// Theme cycler
//------------------------------------------------------------------

// Ordered cycle. First entry has no class (NIGHTOPS = default :root palette).
const THEMES = [
  { cls: '',                       label: 'NIGHTOPS' },
  { cls: 'theme-tron-dark',        label: 'TRON·DARK' },
  { cls: 'theme-tron-light',       label: 'TRON·LIGHT' },
  { cls: 'theme-cyberpunk-dark',   label: 'CYBER·DARK' },
  { cls: 'theme-cyberpunk-light',  label: 'CYBER·LIGHT' },
];

function currentThemeIndex() {
  for (let i = 0; i < THEMES.length; i++) {
    if (THEMES[i].cls && document.body.classList.contains(THEMES[i].cls)) return i;
  }
  return 0;
}

function applyTheme(idx) {
  const next = THEMES[((idx % THEMES.length) + THEMES.length) % THEMES.length];
  // Strip every known theme class, then add the target (skip empty NIGHTOPS class).
  THEMES.forEach(t => { if (t.cls) document.body.classList.remove(t.cls); });
  if (next.cls) document.body.classList.add(next.cls);
  setText('theme-name', next.label);
  try { localStorage.setItem('cw-theme', next.cls); } catch (e) { /* ignore */ }
}

function cycleTheme(delta = 1) {
  applyTheme(currentThemeIndex() + delta);
}

// Init label on load (theme class itself was applied pre-paint by the inline script).
applyTheme(currentThemeIndex());

el('theme-chip').addEventListener('click', () => cycleTheme(1));
document.addEventListener('keydown', (e) => {
  // Skip when typing in scratchpad / other inputs.
  const t = e.target;
  if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) return;
  if (e.key === 't' || e.key === 'T') cycleTheme(e.shiftKey ? -1 : 1);
});

//------------------------------------------------------------------
// Boot
//------------------------------------------------------------------

renderClock();
setInterval(renderClock, 1000);

poll();
setInterval(poll, 5000);
loadScratchpad();
