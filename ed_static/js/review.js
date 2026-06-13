pdfjsLib.GlobalWorkerOptions.workerSrc =
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

let pdfDoc         = null;
let currentPage    = 1;
let totalPages     = 0;
let scale          = 1.0;
let currentViewport = null;
let allIssues      = [];
let selectedId     = null;
let renderTask     = null;
let activeFilter   = 'open';   // 'open' | 'resolved'

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

// Prevent horizontal trackpad/wheel scroll from triggering browser back/forward.
// overscroll-behavior-x: none on .pdf-scroll-area handles most cases; this
// catches events that bubble up to the parent .pdf-panel container first.
const pdfPanel = document.querySelector('.pdf-panel');
[scrollArea, hscrollTop, pdfPanel].forEach(el => {
  if (!el) return;
  el.addEventListener('wheel', (e) => {
    if (e.deltaX !== 0) {
      e.preventDefault();
      scrollArea.scrollLeft += e.deltaX;
      hscrollTop.scrollLeft = scrollArea.scrollLeft;
    }
  }, { passive: false });
});

// ── PDF rendering ────────────────────────────────────

async function renderPage(num) {
  if (!pdfDoc) return;   // PDF still loading — controls are live before it arrives
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

// ── Highlights (disabled for MVP — marker positions too inaccurate) ──────────

function renderHighlights(pageNum) {
  // Highlight overlay disabled: boxes on the drawing confuse users when
  // coordinates are off. Re-enable by restoring the body of this function.
  if (hlLayer) hlLayer.innerHTML = '';
}

function pct(val, dim) { return val / 100 * dim; }

// ── Issue panel ──────────────────────────────────────

function visibleIssues() {
  if (activeFilter === 'resolved') return allIssues.filter(i => i.status === 'resolved');
  return allIssues.filter(i => i.status !== 'resolved');  // 'open' = all non-resolved
}

// Tracks which category groups the user has manually expanded.
// Persists across re-renders so resolving a card doesn't collapse the group.
// On first render, every group starts collapsed except the first (default).
// Once a group has been seen, its state is remembered.
const expandedCategories = new Set();

function renderIssuePanel() {
  const panel = document.getElementById('issue-list');
  const filtered = visibleIssues();

  panel.innerHTML = '';

  if (allIssues.length === 0) {
    panel.innerHTML = '<div class="no-issues-msg"><span class="no-issues-icon">✓</span><strong>No issues found</strong><p>The drawing passed all checks. No errors were found.</p></div>';
    return;
  }

  if (filtered.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'no-issues-msg';
    if (activeFilter === 'resolved') {
      empty.innerHTML = '<span class="no-issues-icon">○</span><strong>Nothing resolved yet</strong><p>Mark issues as resolved as you correct them.</p>';
    } else {
      empty.innerHTML = '<div class="no-issues-msg all-resolved"><span class="no-issues-icon">✓</span><strong>All issues resolved</strong><p>Every flagged item has been marked as resolved. Ready to upload the corrected version.</p></div>';
    }
    panel.appendChild(empty);
    return;
  }

  const openCount = allIssues.filter(i => i.status === 'open').length;
  if (activeFilter === 'open' && openCount === 0 && allIssues.length > 0) {
    panel.innerHTML = '<div class="no-issues-msg all-resolved"><span class="no-issues-icon">✓</span><strong>All issues resolved</strong><p>Every flagged item has been marked as resolved. Ready to upload the corrected version.</p></div>';
    return;
  }

  // Group by category preserving insertion order
  const categories = {};
  filtered.forEach(i => {
    if (!categories[i.category]) categories[i.category] = [];
    categories[i.category].push(i);
  });

  let globalNum = 0;
  let groupIndex = 0;
  Object.entries(categories).forEach(([cat, issues]) => {
    const catOpen = issues.filter(i => i.status !== 'resolved').length;
    const sec = document.createElement('div');
    sec.className = 'issue-category';

    const body = document.createElement('div');
    body.className = 'category-body';
    issues.forEach(issue => {
      globalNum++;
      body.appendChild(buildCard(issue, globalNum));
    });

    // First time this category appears: default first group open, rest closed.
    // After that, honour whatever state the user left it in.
    if (!expandedCategories.has(cat) && groupIndex === 0) expandedCategories.add(cat);
    const isExpanded = expandedCategories.has(cat);
    if (!isExpanded) body.style.display = 'none';

    const hdr = document.createElement('div');
    hdr.className = 'category-header';
    hdr.innerHTML = `
      <span class="category-name">${cat}</span>
      <span class="category-count">${catOpen}</span>
      <span class="caret">${isExpanded ? '▼' : '▶'}</span>`;
    hdr.addEventListener('click', () => {
      const hidden = body.style.display === 'none';
      body.style.display = hidden ? '' : 'none';
      hdr.querySelector('.caret').textContent = hidden ? '▼' : '▶';
      if (hidden) expandedCategories.add(cat); else expandedCategories.delete(cat);
    });

    sec.appendChild(hdr);
    sec.appendChild(body);
    panel.appendChild(sec);
    groupIndex++;
  });
}

function buildCard(issue, num) {
  const resolved = issue.status === 'resolved';
  const card = document.createElement('div');
  card.className = `issue-card sev-${issue.severity}${resolved ? ' resolved' : ''}`;
  card.dataset.id = issue.id;

  card.innerHTML = `
    <div class="issue-title-row">
      <span class="issue-num">#${num}</span>
      <span class="issue-title">${issue.title}</span>
      <button class="resolve-btn" data-id="${issue.id}" title="${resolved ? 'Mark as open' : 'Mark as resolved'}">
        ${resolved ? '↺ Reopen' : '✓ Resolve'}
      </button>
    </div>
    <div class="issue-desc">${issue.description}</div>
    ${issue.suggestion ? `<div class="issue-suggestion">What to do: ${issue.suggestion}</div>` : ''}
  `;

  // Click-to-jump disabled (no highlights shown); only resolve button is active.
  // card.addEventListener('click', (e) => {
  //   if (e.target.closest('.resolve-btn')) return;
  //   goToIssue(issue.id, issue.page_num);
  // });
  card.querySelector('.resolve-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleResolve(issue.id);
  });

  return card;
}

function updateSummary() {
  const open     = allIssues.filter(i => i.status !== 'resolved').length;
  const resolved = allIssues.filter(i => i.status === 'resolved').length;
  document.getElementById('summary-open').textContent     = open;
  document.getElementById('summary-resolved').textContent = resolved;
}

// ── Interactions ──────────────────────────────────────

function selectIssue(id) {
  selectedId = id;
  document.querySelectorAll('.highlight').forEach(el => {
    el.classList.toggle('selected', el.dataset.id === id);
  });
  if (!id) return;
  const card = document.querySelector(`.issue-card[data-id="${id}"]`);
  if (card) {
    card.classList.add('active');
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => card.classList.remove('active'), 1800);
  }
}

// Click on drawing background (not on a highlight) deselects the active marker.
hlLayer.addEventListener('click', (e) => {
  if (!e.target.classList.contains('highlight')) {
    selectIssue(null);
  }
});

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
  if (!hl || !scrollArea) return;

  // Center the highlight in the scroll area both horizontally and vertically
  const hlRect   = hl.getBoundingClientRect();
  const areaRect = scrollArea.getBoundingClientRect();

  const targetLeft = scrollArea.scrollLeft + hlRect.left - areaRect.left
                     - (areaRect.width  - hlRect.width)  / 2;
  const targetTop  = scrollArea.scrollTop  + hlRect.top  - areaRect.top
                     - (areaRect.height - hlRect.height) / 2;

  scrollArea.scrollTo({ left: targetLeft, top: targetTop, behavior: 'smooth' });
  // Keep top scrollbar in sync after the scroll settles
  setTimeout(() => { hscrollTop.scrollLeft = scrollArea.scrollLeft; }, 350);
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

// ── Filter ────────────────────────────────────────────

function setFilter(filter) {
  activeFilter = (activeFilter === filter) ? 'open' : filter;  // clicking active tab resets to open
  document.querySelectorAll('.summary-stat').forEach(el => el.classList.remove('active'));
  const map = { open: 'stat-open', resolved: 'stat-resolved' };
  document.querySelector('.' + map[activeFilter])?.classList.add('active');
  renderIssuePanel();
  renderHighlights(currentPage);
}

document.querySelector('.stat-open')    ?.addEventListener('click', () => setFilter('open'));
document.querySelector('.stat-resolved')?.addEventListener('click', () => setFilter('resolved'));

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

// ── Analysing state — spinner, elapsed timer, skeleton cards ──

let analyseTimer = null;

function showAnalysingState() {
  const issueList = document.getElementById('issue-list');
  const skeleton = `
    <div class="skeleton-card">
      <div class="skeleton-line w40"></div>
      <div class="skeleton-line w90"></div>
      <div class="skeleton-line w70"></div>
    </div>`;
  issueList.innerHTML = `
    <div class="analysing-state">
      <div class="analysing-header">
        <span class="spinner"></span>
        <div class="analysing-text">
          <div class="analysing-title">Analysing drawing… <span class="analysing-elapsed" id="analyse-elapsed">0:00</span></div>
          <div class="analysing-sub">Checking schedule, notes, levels and sections — usually takes 1–2 minutes.
            Results are saved automatically, so you can come back to this page later.</div>
        </div>
      </div>
      ${skeleton}${skeleton}${skeleton}
    </div>`;
  const t0 = Date.now();
  analyseTimer = setInterval(() => {
    const el = document.getElementById('analyse-elapsed');
    if (!el) return;
    const s = Math.floor((Date.now() - t0) / 1000);
    el.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }, 1000);
}

function clearAnalysingState() {
  if (analyseTimer) { clearInterval(analyseTimer); analyseTimer = null; }
  // Reveal the "Upload Corrected Version" CTAs hidden while processing
  const top    = document.getElementById('btn-reupload-top');
  const footer = document.getElementById('panel-footer');
  if (top)    top.style.display = '';
  if (footer) footer.style.display = '';
}

// Tab title shows the outcome so an engineer who tabbed away sees it without
// returning — "5 errors · <drawing> — CASAD ED Checker" / "✓ No errors · …"
const baseTitle = document.title;
function setResultTitle() {
  const open = allIssues.filter(i => i.status !== 'resolved').length;
  document.title = (open ? `${open} error${open === 1 ? '' : 's'}` : '✓ No errors') + ' · ' + baseTitle;
}

// ── Polling — wait for background check to complete ──

async function waitForComplete() {
  while (true) {
    await new Promise(r => setTimeout(r, 4000));
    try {
      const r = await fetch(`/ed/api/review/${window.REVIEW_ID}/status`);
      const d = await r.json();
      if (d.status !== 'processing') break;
    } catch (_) { /* network blip — keep polling */ }
  }
}

// ── Init ──────────────────────────────────────────────

async function loadPdf() {
  pdfDoc = await pdfjsLib.getDocument(window.PDF_URL).promise;
  totalPages = pdfDoc.numPages;
  document.getElementById('total-pages').textContent = totalPages;
  document.getElementById('page-info').style.visibility = '';
  document.getElementById('btn-next').disabled = totalPages <= 1;
  await renderPage(1);

  // Center the drawing horizontally so the user knows it scrolls both ways
  const wrapper = document.getElementById('pdf-wrapper');
  if (wrapper && scrollArea) {
    const overflowX = wrapper.offsetWidth - scrollArea.clientWidth;
    if (overflowX > 0) {
      scrollArea.scrollLeft = overflowX / 2;
      hscrollTop.scrollLeft = scrollArea.scrollLeft;
    }
  }
}

async function init() {
  // Reflect initial defaults in UI
  document.getElementById('zoom-label').textContent = Math.round(scale * 100) + '%';
  document.querySelector('.stat-open')?.classList.add('active');

  // Start the PDF load immediately — the engineer reviews the drawing while
  // the AI check runs. (It previously waited behind the polling loop, leaving
  // an empty grey viewer for the whole 1–2 minute analysis.)
  const pdfReady = loadPdf().catch(err => {
    console.error('ED Checker PDF load error:', err);
    pdfLoader.textContent = 'Failed to load PDF.';
  });

  if (window.REVIEW_STATUS === 'processing') {
    showAnalysingState();
    await waitForComplete();
    clearAnalysingState();
  }

  const resp = await fetch(`/ed/api/review/${window.REVIEW_ID}/issues`);
  allIssues = await resp.json();
  renderIssuePanel();
  updateSummary();
  setResultTitle();

  // Animate the freshly arrived cards in (class is dropped so later
  // re-renders from resolve/filter clicks don't re-animate)
  const panel = document.getElementById('issue-list');
  panel.classList.add('reveal');
  setTimeout(() => panel.classList.remove('reveal'), 1200);

  await pdfReady;
}

init().catch(err => {
  console.error('ED Checker init error:', err);
});
