pdfjsLib.GlobalWorkerOptions.workerSrc =
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

let pdfDoc         = null;
let currentPage    = 1;
let totalPages     = 0;
let scale          = 1.5;
let currentViewport = null;
let allIssues      = [];
let selectedId     = null;
let renderTask     = null;

const canvas      = document.getElementById('pdf-canvas');
const ctx         = canvas.getContext('2d');
const hlLayer     = document.getElementById('highlights-layer');
const pdfLoader   = document.getElementById('pdf-loading');
const scrollArea  = document.getElementById('pdf-scroll-area');
const hscrollTop  = document.getElementById('pdf-hscroll-top');
const hscrollPhantom = document.getElementById('pdf-hscroll-phantom');

// ── Top horizontal scrollbar sync ────────────────────
function syncPhantomWidth() {
  const wrapper = document.getElementById('pdf-wrapper');
  if (wrapper) {
    hscrollPhantom.style.width = wrapper.offsetWidth + 'px';
  }
}
hscrollTop.addEventListener('scroll', () => {
  scrollArea.scrollLeft = hscrollTop.scrollLeft;
});
scrollArea.addEventListener('scroll', () => {
  hscrollTop.scrollLeft = scrollArea.scrollLeft;
});

// ── PDF rendering ────────────────────────────────────

async function renderPage(num) {
  currentPage = num;
  document.getElementById('current-page').textContent = num;
  document.getElementById('btn-prev').disabled = num <= 1;
  document.getElementById('btn-next').disabled = num >= totalPages;

  if (renderTask) { renderTask.cancel(); renderTask = null; }

  const page     = await pdfDoc.getPage(num);
  const viewport = page.getViewport({ scale });
  currentViewport = viewport;

  canvas.width  = viewport.width;
  canvas.height = viewport.height;

  renderTask = page.render({ canvasContext: ctx, viewport });
  try {
    await renderTask.promise;
  } catch (e) {
    if (e.name !== 'RenderingCancelledException') throw e;
    return;
  }

  pdfLoader.style.display = 'none';
  syncPhantomWidth();
  renderHighlights(num);
}

// ── Highlights ───────────────────────────────────────

function renderHighlights(pageNum) {
  if (!currentViewport) return;
  hlLayer.innerHTML = '';
  hlLayer.style.width  = currentViewport.width  + 'px';
  hlLayer.style.height = currentViewport.height + 'px';

  let num = 0;
  allIssues
    .filter(i => i.page_num === pageNum)
    .forEach(issue => {
      num++;
      const div = document.createElement('div');
      div.className = `highlight sev-${issue.severity}${issue.status === 'resolved' ? ' resolved' : ''}${issue.id === selectedId ? ' selected' : ''}`;
      div.style.left   = pct(issue.x,     currentViewport.width)  + 'px';
      div.style.top    = pct(issue.y,     currentViewport.height) + 'px';
      div.style.width  = pct(issue.width, currentViewport.width)  + 'px';
      div.style.height = pct(issue.height,currentViewport.height) + 'px';
      div.dataset.id  = issue.id;
      div.dataset.num = num;
      div.title = issue.title;
      div.addEventListener('click', () => selectIssue(issue.id));
      hlLayer.appendChild(div);
    });
}

function pct(val, dim) { return val / 100 * dim; }

// ── Issue panel ──────────────────────────────────────

function renderIssuePanel() {
  const panel = document.getElementById('issue-list');

  // Group by category preserving insertion order
  const categories = {};
  allIssues.forEach(i => {
    if (!categories[i.category]) categories[i.category] = [];
    categories[i.category].push(i);
  });

  panel.innerHTML = '';

  if (allIssues.length === 0) {
    panel.innerHTML = '<div class="no-issues-msg"><span class="no-issues-icon">✓</span><strong>No issues found</strong><p>The drawing passed all checks. No errors or warnings were raised.</p></div>';
    return;
  }

  const openCount = allIssues.filter(i => i.status === 'open').length;
  if (openCount === 0 && allIssues.length > 0) {
    panel.innerHTML = '<div class="no-issues-msg all-resolved"><span class="no-issues-icon">✓</span><strong>All issues resolved</strong><p>Every flagged item has been marked as resolved. Ready to upload the corrected version.</p></div>';
    return;
  }

  let globalNum = 0;
  Object.entries(categories).forEach(([cat, issues]) => {
    const openCount = issues.filter(i => i.status === 'open').length;
    const sec = document.createElement('div');
    sec.className = 'issue-category';

    const body = document.createElement('div');
    body.className = 'category-body';
    issues.forEach(issue => {
      globalNum++;
      body.appendChild(buildCard(issue, globalNum));
    });

    const hdr = document.createElement('div');
    hdr.className = 'category-header';
    hdr.innerHTML = `
      <span class="category-name">${cat}</span>
      <span class="category-count">${openCount} open</span>
      <span class="caret">▼</span>`;
    hdr.addEventListener('click', () => {
      const hidden = body.style.display === 'none';
      body.style.display = hidden ? '' : 'none';
      hdr.querySelector('.caret').textContent = hidden ? '▼' : '▶';
    });

    sec.appendChild(hdr);
    sec.appendChild(body);
    panel.appendChild(sec);
  });
}

function buildCard(issue, num) {
  const resolved = issue.status === 'resolved';
  const card = document.createElement('div');
  card.className = `issue-card${resolved ? ' resolved' : ''}`;
  card.dataset.id = issue.id;

  card.innerHTML = `
    <div class="issue-header">
      <span class="sev-badge sev-${issue.severity}">${issue.severity === 'error' ? '✕ Error' : '⚠ Warning'}</span>
      <button class="resolve-btn" data-id="${issue.id}" title="${resolved ? 'Mark as open' : 'Mark as resolved'}">
        ${resolved ? '↺ Reopen' : '✓ Resolve'}
      </button>
    </div>
    <div class="issue-num">#${num}</div>
    <div class="issue-title">${issue.title}</div>
    <div class="issue-desc">${issue.description}</div>
    ${issue.suggestion ? `<div class="issue-suggestion">💡 ${issue.suggestion}</div>` : ''}
  `;

  card.addEventListener('click', (e) => {
    if (e.target.closest('.resolve-btn')) return;
    goToIssue(issue.id, issue.page_num);
  });
  card.querySelector('.resolve-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleResolve(issue.id);
  });

  return card;
}

function updateSummary() {
  const errors   = allIssues.filter(i => i.severity === 'error'   && i.status === 'open').length;
  const warnings = allIssues.filter(i => i.severity === 'warning' && i.status === 'open').length;
  const resolved = allIssues.filter(i => i.status === 'resolved').length;
  document.getElementById('summary-errors').textContent   = errors;
  document.getElementById('summary-warnings').textContent = warnings;
  document.getElementById('summary-resolved').textContent = resolved;
}

// ── Interactions ──────────────────────────────────────

function selectIssue(id) {
  selectedId = id;
  document.querySelectorAll('.highlight').forEach(el => {
    el.classList.toggle('selected', el.dataset.id === id);
  });
  const card = document.querySelector(`.issue-card[data-id="${id}"]`);
  if (card) {
    card.classList.add('active');
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => card.classList.remove('active'), 1800);
  }
}

function goToIssue(id, pageNum) {
  selectedId = id;
  if (pageNum !== currentPage) {
    renderPage(pageNum).then(() => {
      highlightSelected(id);
      scrollToHighlight(id);
    });
  } else {
    highlightSelected(id);
    scrollToHighlight(id);
  }
}

function highlightSelected(id) {
  document.querySelectorAll('.highlight').forEach(el => {
    el.classList.toggle('selected', el.dataset.id === id);
  });
}

function scrollToHighlight(id) {
  const hl = document.querySelector(`.highlight[data-id="${id}"]`);
  if (hl) hl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function toggleResolve(id) {
  const issue = allIssues.find(i => i.id === id);
  if (!issue) return;
  const newStatus = issue.status === 'open' ? 'resolved' : 'open';
  await fetch(`/ed/api/issues/${id}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: newStatus }),
  });
  issue.status = newStatus;
  renderIssuePanel();
  updateSummary();
  renderHighlights(currentPage);
}

// ── Controls ──────────────────────────────────────────

document.getElementById('btn-prev').addEventListener('click', () => {
  if (currentPage > 1) renderPage(currentPage - 1);
});
document.getElementById('btn-next').addEventListener('click', () => {
  if (currentPage < totalPages) renderPage(currentPage + 1);
});
document.getElementById('btn-zoom-in').addEventListener('click', () => {
  scale = Math.min(scale + 0.25, 3.0);
  document.getElementById('zoom-label').textContent = Math.round(scale * 100) + '%';
  renderPage(currentPage);
});
document.getElementById('btn-zoom-out').addEventListener('click', () => {
  scale = Math.max(scale - 0.25, 0.5);
  document.getElementById('zoom-label').textContent = Math.round(scale * 100) + '%';
  renderPage(currentPage);
});

// ── Init ──────────────────────────────────────────────

async function init() {
  // Load issues first so highlights appear as soon as PDF renders
  const resp = await fetch(`/ed/api/review/${window.REVIEW_ID}/issues`);
  allIssues = await resp.json();
  renderIssuePanel();
  updateSummary();

  // Load PDF
  pdfDoc = await pdfjsLib.getDocument(window.PDF_URL).promise;
  totalPages = pdfDoc.numPages;
  document.getElementById('total-pages').textContent = totalPages;
  await renderPage(1);
}

init().catch(err => {
  console.error('ED Checker init error:', err);
  document.getElementById('pdf-loading').textContent = 'Failed to load PDF.';
});
