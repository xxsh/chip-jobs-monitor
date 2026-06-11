const metricsEl = document.querySelector('#metrics');
const companyStripEl = document.querySelector('#companyStrip');
const statusEl = document.querySelector('#status');
const rangeLabelEl = document.querySelector('#rangeLabel');
const activeChartEl = document.querySelector('#activeChart');
const sourceChartEl = document.querySelector('#sourceChart');
const fitChartEl = document.querySelector('#fitChart');
const rolesBodyEl = document.querySelector('#rolesBody');
const roleCompanyEl = document.querySelector('#roleCompany');
const roleFitEl = document.querySelector('#roleFit');
const roleSortEl = document.querySelector('#roleSort');
const roleSearchEl = document.querySelector('#roleSearch');
const roleCountEl = document.querySelector('#roleCount');
const chartTipEl = document.querySelector('#chartTip');
const topRolesEl = document.querySelector('#topRoles');
const recentAddsEl = document.querySelector('#recentAdds');
const recentCancelsEl = document.querySelector('#recentCancels');

let allRoles = [];

const sourceColors = {
  amd: '#d3352f',
  arm: '#008c95',
  intel: '#2468b2',
  nvidia: '#6aa619',
};

const fallbackColors = ['#5c6f7b', '#a15b2b', '#7d4d8b', '#2f8173'];

const fitColors = {
  strongFit: '#1f8a5b',
  goodFit: '#1b8aa5',
  possibleStretch: '#d59b2d',
  lowFit: '#747a71',
};

function fmt(value) {
  return new Intl.NumberFormat('en-US').format(Number(value ?? 0));
}

function fmtSigned(value) {
  const number = Number(value ?? 0);
  return `${number >= 0 ? '+' : ''}${fmt(number)}`;
}

function shortDate(value) {
  return value?.slice(5) ?? '';
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function colorForSource(source) {
  if (sourceColors[source]) return sourceColors[source];
  const key = String(source || 'unknown');
  const index = [...key].reduce((sum, char) => sum + char.charCodeAt(0), 0) % fallbackColors.length;
  return fallbackColors[index];
}

function pointsToPath(points) {
  return points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
}

function renderMetrics(summary) {
  const scoreMode = summary.scoreMode === 'latest-score-event' ? 'latest score event' : 'latest resume';
  const cards = [
    ['Active roles', fmt(summary.activeJobs), `${fmtSigned(summary.activeDelta)} vs previous source runs`],
    ['Companies', fmt(summary.sourceCount), `${summary.location || 'All locations'} · latest ${summary.latestDate || 'no runs'}`],
    ['Latest added', fmt(summary.latestAdded), `${fmt(summary.totalAdded)} added across history`],
    ['Latest canceled', fmt(summary.latestCanceled), `${fmt(summary.totalCanceled)} canceled across history`],
    ['Latest scores', fmt(summary.latestScoreCount ?? 0), `${fmt(summary.scoreEventCount ?? 0)} score events retained`],
    ['Best score', summary.bestScore ?? 'N/A', scoreMode],
  ];
  metricsEl.innerHTML = cards.map(([label, value, note]) => `
    <article class="metric">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
      <div class="note">${escapeHtml(note)}</div>
    </article>
  `).join('');
}

function renderCompanyStrip(sources) {
  if (!sources.length) {
    companyStripEl.innerHTML = '<div class="empty">No company data</div>';
    return;
  }

  companyStripEl.innerHTML = sources.map((source) => `
    <article class="source-card" style="--source-color: ${colorForSource(source.source)}">
      <div class="source-card-head">
        <span class="source-mark"></span>
        <div>
          <h3>${escapeHtml(source.display)}</h3>
          <p>${escapeHtml(source.latestDate || 'No runs')}</p>
        </div>
      </div>
      <div class="source-active">${fmt(source.activeJobs)}</div>
      <div class="source-card-grid">
        <span>${source.isNewSource ? 'New coverage' : `${fmtSigned(source.activeDelta)} active`}</span>
        <span>+${fmt(source.latestAdded)} added</span>
        <span>-${fmt(source.latestCanceled)} canceled</span>
        <span>${source.bestScore == null ? 'N/A' : source.bestScore} best</span>
      </div>
    </article>
  `).join('');
}

function renderActiveChart(runs) {
  if (!runs.length) {
    activeChartEl.innerHTML = '<div class="empty">No run data</div>';
    return;
  }

  const width = 940;
  const height = 320;
  const padding = { top: 22, right: 28, bottom: 42, left: 52 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const maxActive = Math.max(...runs.map((row) => Number(row.activeJobs)), 1);
  const minActive = Math.min(...runs.map((row) => Number(row.activeJobs)), maxActive);
  const maxDelta = Math.max(...runs.map((row) => Math.max(Number(row.added), Number(row.canceled))), 1);
  const span = maxActive - minActive;
  const xStep = runs.length > 1 ? plotW / (runs.length - 1) : plotW;
  const barW = Math.max(5, Math.min(16, plotW / Math.max(1, runs.length) / 3));

  const points = runs.map((row, index) => ({
    x: padding.left + index * xStep,
    y: span
      ? padding.top + plotH - ((Number(row.activeJobs) - minActive) / span) * plotH
      : padding.top + plotH * 0.45,
  }));

  const bars = runs.map((row, index) => {
    const x = padding.left + index * xStep;
    const addedH = (Number(row.added) / maxDelta) * 82;
    const canceledH = (Number(row.canceled) / maxDelta) * 82;
    const base = height - padding.bottom;
    return `
      <rect class="added-bar" x="${x - barW - 1}" y="${base - addedH}" width="${barW}" height="${addedH}" rx="2"></rect>
      <rect class="canceled-bar" x="${x + 1}" y="${base - canceledH}" width="${barW}" height="${canceledH}" rx="2"></rect>
    `;
  }).join('');

  const labelIndexes = [...new Set([
    0,
    Math.round((runs.length - 1) * 0.25),
    Math.round((runs.length - 1) * 0.5),
    Math.round((runs.length - 1) * 0.75),
    runs.length - 1,
  ])].sort((a, b) => a - b);
  const labels = labelIndexes
    .map((originalIndex, index) => {
      const row = runs[originalIndex];
      const x = padding.left + originalIndex * xStep;
      const anchor = index === 0 ? 'start' : index === labelIndexes.length - 1 ? 'end' : 'middle';
      return `<text class="chart-label" x="${x}" y="${height - 14}" text-anchor="${anchor}">${shortDate(row.date)}</text>`;
    }).join('');

  const latest = runs.at(-1);
  const coverageExpanded = runs.some((row, index) => index > 0 && Number(row.sourceCount) > Number(runs[index - 1].sourceCount));
  activeChartEl.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Tracked active jobs and daily added or canceled trend">
      <line class="axis" x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}"></line>
      <line class="axis" x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}"></line>
      ${bars}
      <path class="active-line" d="${pointsToPath(points)}"></path>
      ${points.map((point) => `<circle class="dot" cx="${point.x}" cy="${point.y}" r="4"></circle>`).join('')}
      ${points.map((point, index) => {
        const row = runs[index];
        const tip = `${row.date}\nactive ${fmt(row.activeJobs)}\n+${fmt(row.added)} added · -${fmt(row.canceled)} canceled`;
        return `<circle class="hit" cx="${point.x}" cy="${point.y}" r="11" data-tip="${escapeHtml(tip)}"></circle>`;
      }).join('')}
      <text class="chart-label" x="${padding.left}" y="${padding.top + 6}">${fmt(maxActive)} active max</text>
      <text class="chart-label" x="${padding.left}" y="${height - padding.bottom - 92}">green posted / red canceled</text>
      <text class="chart-label" x="${width - padding.right}" y="${padding.top + 6}" text-anchor="end">${coverageExpanded ? 'line includes onboarding' : `${fmt(latest.sourceCount)} sources latest`}</text>
      ${labels}
    </svg>
  `;
}

function renderSourceChart(sources) {
  if (!sources.length) {
    sourceChartEl.innerHTML = '<div class="empty">No source data</div>';
    return;
  }

  const maxActive = Math.max(...sources.map((source) => Number(source.activeJobs)), 1);
  sourceChartEl.innerHTML = sources.map((source) => {
    const width = Math.max(4, (Number(source.activeJobs) / maxActive) * 100);
    return `
      <div class="source-row" style="--source-color: ${colorForSource(source.source)}">
        <div class="source-row-head">
          <span>${escapeHtml(source.display)}</span>
          <strong>${fmt(source.activeJobs)}</strong>
        </div>
        <div class="source-bar"><span style="width: ${width.toFixed(2)}%"></span></div>
        <div class="source-row-meta">
          <span>${source.daysTracked}d</span>
          <span>+${fmt(source.latestAdded)}</span>
          <span>-${fmt(source.latestCanceled)}</span>
          <span>${source.queued ? `${fmt(source.queued)} queued` : 'clear'}</span>
        </div>
      </div>
    `;
  }).join('');
}

function renderFitChart(series) {
  if (!series.length) {
    fitChartEl.innerHTML = '<div class="empty">No score data</div>';
    return;
  }

  const width = 520;
  const height = 260;
  const padding = { top: 18, right: 20, bottom: 36, left: 22 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const maxTotal = Math.max(...series.map((row) => row.strongFit + row.goodFit + row.possibleStretch + row.lowFit), 1);
  const slotW = plotW / Math.max(1, series.length);
  const barW = Math.max(7, Math.min(18, slotW - 4));

  const bars = series.map((row, index) => {
    const x = padding.left + index * slotW + Math.max(0, (slotW - barW) / 2);
    let y = height - padding.bottom;
    const labels = { strongFit: 'Strong', goodFit: 'Good', possibleStretch: 'Stretch', lowFit: 'Low' };
    return ['lowFit', 'possibleStretch', 'goodFit', 'strongFit'].map((key) => {
      const h = (row[key] / maxTotal) * plotH;
      y -= h;
      const tip = `${row.date}\n${labels[key]} fit: ${fmt(row[key])}`;
      return `<rect class="fit-seg" x="${x}" y="${y}" width="${barW}" height="${h}" fill="${fitColors[key]}" rx="2" data-tip="${escapeHtml(tip)}"></rect>`;
    }).join('');
  }).join('');

  fitChartEl.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Scored job fit distribution over time">
      <line class="axis" x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}"></line>
      ${bars}
      <text class="chart-label" x="${padding.left}" y="${height - 12}">${shortDate(series[0]?.date)}</text>
      <text class="chart-label" x="${width - padding.right}" y="${height - 12}" text-anchor="end">${shortDate(series.at(-1)?.date)}</text>
      <text class="chart-label" x="${padding.left}" y="${padding.top + 4}">strong / good / stretch / low</text>
    </svg>
    <div class="fit-legend">
      <span><i class="legend-strong"></i>Strong</span>
      <span><i class="legend-good"></i>Good</span>
      <span><i class="legend-stretch"></i>Stretch</span>
      <span><i class="legend-low"></i>Low</span>
    </div>
  `;
}

function suitabilityClass(value) {
  if (value === 'Strong fit') return 'strong';
  if (value === 'Good fit') return 'good';
  if (value === 'Possible stretch') return 'stretch';
  return 'low';
}

function sourcePill(row) {
  return `<span class="source-pill" style="--source-color: ${colorForSource(row.source)}">${escapeHtml(row.sourceDisplay || row.source)}</span>`;
}

function statusPill(row) {
  const status = row.status === 'cancelled' ? 'cancelled' : 'valid';
  return `<span class="status-pill ${status}">${status}</span>`;
}

const FIT_ORDER = { 'Strong fit': 0, 'Good fit': 1, 'Possible stretch': 2, 'Low fit': 3 };

function recClass(rec) {
  if (rec === 'Apply') return 'rec-apply';
  if (rec === 'Maybe') return 'rec-maybe';
  return 'rec-skip';
}

function deltaCell(role) {
  if (role.previousScore == null || role.scoreDelta == null) {
    return '<span class="delta-none">—</span>';
  }
  const d = Number(role.scoreDelta);
  const cls = d > 0 ? 'delta-up' : d < 0 ? 'delta-down' : 'delta-flat';
  const arrow = d > 0 ? '▲' : d < 0 ? '▼' : '·';
  return `<span class="delta ${cls}" title="previous score event was ${escapeHtml(role.previousScore)}">${arrow} ${d >= 0 ? '+' : ''}${d}</span>`;
}

function scoreDeltaPill(role) {
  if (role.previousScore == null || role.scoreDelta == null) return '';
  const delta = Number(role.scoreDelta);
  return `<span class="score-delta">prev ${escapeHtml(role.previousScore)} · ${delta >= 0 ? '+' : ''}${escapeHtml(delta)}</span>`;
}

function scoreSourcePill(role) {
  if (role.scoreSource === 'resume_scores') return '<span class="profile-pill">manual rescore</span>';
  if (role.scoreSource === 'scores') return '<span class="profile-pill">daily score</span>';
  return '';
}

function renderTopRoles(roles) {
  topRolesEl.innerHTML = roles.slice(0, 12).map((role) => `
    <li>
      <a href="${escapeHtml(role.link || '#')}" target="_blank" rel="noreferrer">${escapeHtml(role.title)}</a>
      <div class="meta">
        ${sourcePill(role)}
        ${statusPill(role)}
        <span class="pill ${suitabilityClass(role.suitability)}">${escapeHtml(role.score)} · ${escapeHtml(role.suitability)}</span>
        ${scoreSourcePill(role)}
        ${scoreDeltaPill(role)}
        <span>${escapeHtml(role.jr)}</span>
        <span>${escapeHtml(role.date)}</span>
      </div>
    </li>
  `).join('') || '<li class="empty">No scored roles</li>';
}

function renderRolesTable() {
  const company = roleCompanyEl.value;
  const fit = roleFitEl.value;
  const sort = roleSortEl.value;
  const query = roleSearchEl.value.trim().toLowerCase();

  const rows = allRoles
    .filter((role) => {
      if (company && role.source !== company) return false;
      if (fit && role.suitability !== fit) return false;
      if (query) {
        const hay = `${role.title || ''} ${role.jr || ''} ${role.department || ''}`.toLowerCase();
        if (!hay.includes(query)) return false;
      }
      return true;
    })
    .sort((a, b) => {
      if (sort === 'delta') {
        const da = a.scoreDelta == null ? -Infinity : Number(a.scoreDelta);
        const db = b.scoreDelta == null ? -Infinity : Number(b.scoreDelta);
        return db - da || Number(b.score) - Number(a.score);
      }
      if (sort === 'date') {
        return String(b.date || '').localeCompare(String(a.date || '')) || Number(b.score) - Number(a.score);
      }
      return Number(b.score) - Number(a.score) || (FIT_ORDER[a.suitability] ?? 9) - (FIT_ORDER[b.suitability] ?? 9);
    });

  roleCountEl.textContent = `${rows.length} of ${allRoles.length}`;
  rolesBodyEl.innerHTML = rows.map((role) => `
    <tr>
      <td class="num"><span class="score-chip ${suitabilityClass(role.suitability)}">${escapeHtml(role.score)}</span></td>
      <td><span class="source-pill" style="--source-color: ${colorForSource(role.source)}">${escapeHtml(role.sourceDisplay || role.source)}</span></td>
      <td class="fit-cell">${escapeHtml(role.suitability || '—')}</td>
      <td class="role-cell">
        <a href="${escapeHtml(role.link || '#')}" target="_blank" rel="noreferrer">${escapeHtml(role.title)}</a>
        ${role.department ? `<span class="role-dept">${escapeHtml(role.department)}</span>` : ''}
      </td>
      <td><span class="rec ${recClass(role.recommendation)}">${escapeHtml(role.recommendation || '—')}</span></td>
      <td class="num">${deltaCell(role)}</td>
      <td class="mono">${escapeHtml(role.jr || '—')}</td>
      <td class="mono">${escapeHtml(role.date || '—')}</td>
    </tr>
  `).join('') || '<tr><td colspan="8" class="empty">No roles match these filters</td></tr>';
}

function setupRolesControls(sources) {
  roleCompanyEl.innerHTML = ['<option value="">All companies</option>']
    .concat((sources || []).map((s) => `<option value="${escapeHtml(s.source)}">${escapeHtml(s.display)}</option>`))
    .join('');
  for (const el of [roleCompanyEl, roleFitEl, roleSortEl]) el.onchange = renderRolesTable;
  roleSearchEl.oninput = renderRolesTable;
}

function showTip(text, x, y) {
  chartTipEl.textContent = text;
  chartTipEl.hidden = false;
  const pad = 14;
  const rect = chartTipEl.getBoundingClientRect();
  let left = x + pad;
  let top = y + pad;
  if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
  if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
  chartTipEl.style.left = `${Math.max(8, left)}px`;
  chartTipEl.style.top = `${Math.max(8, top)}px`;
}

function attachChartTips(containerEl) {
  containerEl.addEventListener('mousemove', (event) => {
    const target = event.target.closest('[data-tip]');
    if (!target) {
      chartTipEl.hidden = true;
      return;
    }
    showTip(target.getAttribute('data-tip'), event.clientX, event.clientY);
  });
  containerEl.addEventListener('mouseleave', () => {
    chartTipEl.hidden = true;
  });
}

function renderChanges(el, rows, { linked = false } = {}) {
  el.innerHTML = rows.map((row) => `
    <li>
      ${linked && row.link
        ? `<a href="${escapeHtml(row.link)}" target="_blank" rel="noreferrer">${escapeHtml(row.title)}</a>`
        : `<span class="change-title">${escapeHtml(row.title)}</span>`}
      <div class="meta">
        ${sourcePill(row)}
        <span>${escapeHtml(row.date)}</span>
        <span>${escapeHtml(row.jr || 'no JR')}</span>
        ${row.department ? `<span>${escapeHtml(row.department)}</span>` : ''}
      </div>
    </li>
  `).join('') || '<li class="empty">No changes</li>';
}

async function loadDashboard() {
  try {
    const response = await fetch('/api/trends');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();

    statusEl.textContent = `Updated ${new Date(data.generatedAt).toLocaleString()}`;
    rangeLabelEl.textContent = data.summary.firstDate
      ? `${data.summary.firstDate} -> ${data.summary.latestDate} · ${fmt(data.summary.sourceCount)} companies`
      : '';
    renderMetrics(data.summary);
    renderCompanyStrip(data.sources || []);
    renderActiveChart(data.runs || []);
    renderSourceChart(data.sources || []);
    renderFitChart(data.fitSeries || []);
    allRoles = data.roles || [];
    renderTopRoles(allRoles);
    setupRolesControls(data.sources || []);
    renderRolesTable();
    renderChanges(recentAddsEl, data.recentAdds || [], { linked: true });
    renderChanges(recentCancelsEl, data.recentCancels || []);
  } catch (error) {
    statusEl.textContent = 'Dashboard error';
    metricsEl.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
    companyStripEl.innerHTML = '';
  }
}

loadDashboard();
