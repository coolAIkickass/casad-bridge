// This script is shared by upload.html AND reupload.html, which use two different
// markup shapes:
//   - reupload.html: one static PDF/DXF drop-zone pair with fixed legacy IDs
//     (#drop-zone, #file-input, ...) — "Mode A" below.
//   - upload.html: a repeatable list of drawing rows (#drawing-rows, cloned from
//     #drawing-row-template) so one design input can be checked against several
//     drawings in one submission — "Mode B" below.
// Each mode's block is guarded on the container element it needs, so only one
// runs per page; the Design Inputs slot (shared by both, absent on reupload.html)
// is wired once at the bottom regardless of mode.

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

const submitBtn    = document.getElementById('submit-btn');
const uploadForm   = document.getElementById('upload-form');
const waitNote     = document.getElementById('review-wait-note');
const submitBtnDefaultText = submitBtn.textContent.trim();

function startSubmitUI(label) {
  submitBtn.disabled = true;
  submitBtn.textContent = label || submitBtnDefaultText;
  submitBtn.classList.add('reviewing');
  if (waitNote) waitNote.style.display = '';
}

// DXF is ASCII text — gzip cuts a 25 MB file to ~6 MB, so the upload fits inside
// Render's proxy window on slow office connections. The server accepts ".dxf.gz"
// and gunzips (_read_dxf_upload in ed_blueprint.py). Falls back to the raw file
// if the browser has no CompressionStream (old browsers) or compression fails.
async function gzipFile(file) {
  try {
    if (typeof CompressionStream === 'undefined') return file;
    const gzStream = file.stream().pipeThrough(new CompressionStream('gzip'));
    const gzBlob   = await new Response(gzStream).blob();
    return new File([gzBlob], file.name + '.gz', { type: 'application/gzip' });
  } catch (err) {
    return file;
  }
}

// ── Mode A: reupload.html — single static drop-zone pair, legacy IDs ─────────
const dropZone = document.getElementById('drop-zone');
if (dropZone) {
  const fileInput    = document.getElementById('file-input');
  const dropLabel    = document.getElementById('drop-label');
  const dropSelected = document.getElementById('drop-selected');
  const filenameTxt  = document.getElementById('selected-filename');

  function setFile(file) {
    if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
      alert('Please select a PDF file for the drawing.');
      return;
    }
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    filenameTxt.textContent = file.name;
    const sizeTxt = document.getElementById('selected-size');
    if (sizeTxt) sizeTxt.textContent = formatSize(file.size);
    dropLabel.style.display    = 'none';
    dropSelected.style.display = '';
    submitBtn.disabled = false;
    submitBtn.textContent = submitBtnDefaultText;

    const nameInput = document.getElementById('drawing_name');
    if (nameInput) {
      const raw = file.name.replace(/\.pdf$/i, '').replace(/[_\-]+/g, ' ').trim();
      nameInput.value = raw.length > 60 ? raw.slice(0, 57) + '…' : raw;
    }

    const nextZone = dxfDropZone || designDropZone;
    if (nextZone) {
      nextZone.classList.remove('attention');
      void nextZone.offsetWidth;  // force reflow to restart animation
      nextZone.classList.add('attention');
      nextZone.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      nextZone.addEventListener('animationend', () => {
        nextZone.classList.remove('attention');
      }, { once: true });
    }
  }

  function clearFile() {
    fileInput.value = '';
    dropLabel.style.display    = '';
    dropSelected.style.display = 'none';
    submitBtn.disabled = true;
    submitBtn.textContent = submitBtnDefaultText;
    const nameInput = document.getElementById('drawing_name');
    if (nameInput) nameInput.value = '';
  }
  window.clearFile = clearFile;  // reupload.html's Remove button uses inline onclick

  fileInput.addEventListener('change', () => { if (fileInput.files[0]) setFile(fileInput.files[0]); });

  dropZone.addEventListener('click', (e) => { if (!e.target.closest('button')) fileInput.click(); });
  dropZone.addEventListener('dragover',  (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
  dropZone.addEventListener('dragleave', ()  => dropZone.classList.remove('dragover'));
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });

  // Single AutoCAD DXF slot
  const dxfInput    = document.getElementById('dxf-input');
  const dxfList     = document.getElementById('dxf-file-list');
  const dxfDropZone = document.getElementById('dxf-drop-zone');
  let dxfFile  = null;
  let dxfReady = false;

  function renderDxfFile() {
    dxfList.innerHTML = '';
    if (!dxfFile) return;
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="file-row">
        <span class="file-row-icon">DXF</span>
        <span class="file-row-name">${dxfFile.name}</span>
        <span class="file-row-meta">${formatSize(dxfFile.size)}</span>
        <button type="button" class="file-row-remove" title="Remove">✕</button>
      </div>`;
    li.querySelector('.file-row-remove').addEventListener('click', () => {
      dxfFile = null;
      dxfReady = false;
      dxfInput.value = '';
      renderDxfFile();
    });
    dxfList.appendChild(li);
  }

  if (dxfInput) {
    dxfInput.addEventListener('change', () => {
      if (dxfInput.files[0]) { dxfFile = dxfInput.files[0]; dxfReady = false; renderDxfFile(); }
    });
    if (dxfDropZone) {
      dxfDropZone.addEventListener('click', (e) => { if (!e.target.closest('button, li')) dxfInput.click(); });
      dxfDropZone.addEventListener('dragover',  (e) => { e.preventDefault(); dxfDropZone.classList.add('dragover'); });
      dxfDropZone.addEventListener('dragleave', ()  => dxfDropZone.classList.remove('dragover'));
      dxfDropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dxfDropZone.classList.remove('dragover');
        const f = e.dataTransfer.files[0];
        if (f && f.name.toLowerCase().endsWith('.dxf')) { dxfFile = f; dxfReady = false; renderDxfFile(); }
      });
    }
  }

  uploadForm.addEventListener('submit', (e) => {
    if (designInput && designFiles.length === 0) {
      const ok = window.confirm(
        'No design input file added.\n\n' +
        'Without it, only title-block format and schedule arithmetic can be checked — ' +
        'reinforcement schedule comparisons will be skipped.\n\n' +
        'Continue without design input?'
      );
      if (!ok) {
        e.preventDefault();
        designDropZone.scrollIntoView({ behavior: 'smooth', block: 'center' });
        designDropZone.classList.remove('attention');
        void designDropZone.offsetWidth;
        designDropZone.classList.add('attention');
        return;
      }
    }
    if (dxfFile && !dxfReady) {
      e.preventDefault();
      (async () => {
        startSubmitUI('Compressing DXF...');
        const outFile = await gzipFile(dxfFile);
        const dt = new DataTransfer();
        dt.items.add(outFile);
        dxfInput.files = dt.files;
        dxfReady = true;
        startSubmitUI();
        uploadForm.submit();  // bypasses this listener — no recursion
      })();
      return;
    }
    startSubmitUI();
  });
}

// ── Mode B: upload.html — repeatable drawing rows, one shared design input ───
const rowsContainer = document.getElementById('drawing-rows');
const rowTemplate   = document.getElementById('drawing-row-template');
if (rowsContainer && rowTemplate) {
  const addRowBtn = document.getElementById('add-drawing-row-btn');
  const rows = [];  // { el, pdfFile, dxfFile, dxfReady }

  function renumberRows() {
    rows.forEach((r, i) => {
      // Re-derive name="<base>_<index>" from each field's data-name — this runs
      // after every add/remove so indices stay 0..N-1 with no gaps, which the
      // server's _read_drawing_rows() relies on to know when the batch ends.
      r.el.querySelectorAll('[data-name]').forEach((el) => {
        el.name = el.dataset.name + '_' + i;
      });
      const titleEl = r.el.querySelector('.drawing-row-title');
      if (titleEl) titleEl.textContent = 'Drawing ' + (i + 1);
      const removeBtn = r.el.querySelector('.drawing-row-remove');
      if (removeBtn) removeBtn.style.display = rows.length > 1 ? '' : 'none';
    });
    if (!submitBtn.classList.contains('reviewing')) {
      submitBtn.textContent = rows.length > 1 ? `Review ${rows.length} drawings` : 'Review drawing';
    }
  }

  function updateSubmitState() {
    submitBtn.disabled = !(rows.length > 0 && rows.every((r) => r.pdfFile));
  }

  function setRowPdf(row, file) {
    if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
      alert('Please select a PDF file for the drawing.');
      return;
    }
    row.pdfFile = file;
    const el = row.el;
    const dt = new DataTransfer();
    dt.items.add(file);
    el.querySelector('.js-file-input').files = dt.files;
    el.querySelector('.js-selected-filename').textContent = file.name;
    el.querySelector('.js-selected-size').textContent = formatSize(file.size);
    el.querySelector('.js-drop-label').style.display    = 'none';
    el.querySelector('.js-drop-selected').style.display = '';

    const raw = file.name.replace(/\.pdf$/i, '').replace(/[_\-]+/g, ' ').trim();
    el.querySelector('.js-drawing-name').value = raw.length > 60 ? raw.slice(0, 57) + '…' : raw;

    const dxfZone = el.querySelector('.js-dxf-drop-zone');
    dxfZone.classList.remove('attention');
    void dxfZone.offsetWidth;
    dxfZone.classList.add('attention');
    dxfZone.addEventListener('animationend', () => dxfZone.classList.remove('attention'), { once: true });

    updateSubmitState();
  }

  function clearRowPdf(row) {
    row.pdfFile = null;
    const el = row.el;
    el.querySelector('.js-file-input').value = '';
    el.querySelector('.js-drop-label').style.display    = '';
    el.querySelector('.js-drop-selected').style.display = 'none';
    el.querySelector('.js-drawing-name').value = '';
    updateSubmitState();
  }

  function renderRowDxf(row) {
    const el   = row.el;
    const list = el.querySelector('.js-dxf-file-list');
    list.innerHTML = '';
    if (!row.dxfFile) return;
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="file-row">
        <span class="file-row-icon">DXF</span>
        <span class="file-row-name">${row.dxfFile.name}</span>
        <span class="file-row-meta">${formatSize(row.dxfFile.size)}</span>
        <button type="button" class="file-row-remove" title="Remove">✕</button>
      </div>`;
    li.querySelector('.file-row-remove').addEventListener('click', () => {
      row.dxfFile = null;
      row.dxfReady = false;
      el.querySelector('.js-dxf-input').value = '';
      renderRowDxf(row);
    });
    list.appendChild(li);
  }

  function wireRow(row) {
    const el = row.el;

    const pdfZone  = el.querySelector('.js-drop-zone');
    const pdfInput = el.querySelector('.js-file-input');
    el.querySelectorAll('.js-browse-pdf').forEach((btn) =>
      btn.addEventListener('click', () => pdfInput.click()));
    pdfInput.addEventListener('change', () => { if (pdfInput.files[0]) setRowPdf(row, pdfInput.files[0]); });
    el.querySelector('.js-clear-file').addEventListener('click', () => clearRowPdf(row));
    pdfZone.addEventListener('click', (e) => { if (!e.target.closest('button')) pdfInput.click(); });
    pdfZone.addEventListener('dragover',  (e) => { e.preventDefault(); pdfZone.classList.add('dragover'); });
    pdfZone.addEventListener('dragleave', ()  => pdfZone.classList.remove('dragover'));
    pdfZone.addEventListener('drop', (e) => {
      e.preventDefault();
      pdfZone.classList.remove('dragover');
      if (e.dataTransfer.files[0]) setRowPdf(row, e.dataTransfer.files[0]);
    });

    const dxfZone  = el.querySelector('.js-dxf-drop-zone');
    const dxfInput = el.querySelector('.js-dxf-input');
    el.querySelectorAll('.js-browse-dxf').forEach((btn) =>
      btn.addEventListener('click', () => dxfInput.click()));
    dxfInput.addEventListener('change', () => {
      if (dxfInput.files[0]) { row.dxfFile = dxfInput.files[0]; row.dxfReady = false; renderRowDxf(row); }
    });
    dxfZone.addEventListener('click', (e) => { if (!e.target.closest('button, li')) dxfInput.click(); });
    dxfZone.addEventListener('dragover',  (e) => { e.preventDefault(); dxfZone.classList.add('dragover'); });
    dxfZone.addEventListener('dragleave', ()  => dxfZone.classList.remove('dragover'));
    dxfZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dxfZone.classList.remove('dragover');
      const f = e.dataTransfer.files[0];
      if (f && f.name.toLowerCase().endsWith('.dxf')) { row.dxfFile = f; row.dxfReady = false; renderRowDxf(row); }
    });

    el.querySelector('.drawing-row-remove').addEventListener('click', () => removeRow(row));
  }

  function addRow() {
    const frag  = rowTemplate.content.cloneNode(true);
    const rowEl = frag.querySelector('.drawing-row');
    rowsContainer.appendChild(rowEl);
    const row = { el: rowEl, pdfFile: null, dxfFile: null, dxfReady: false };
    rows.push(row);
    wireRow(row);
    renumberRows();
    updateSubmitState();
    return row;
  }

  function removeRow(row) {
    if (rows.length <= 1) return;  // always keep at least one drawing row
    row.el.remove();
    const i = rows.indexOf(row);
    if (i >= 0) rows.splice(i, 1);
    renumberRows();
    updateSubmitState();
  }

  addRowBtn.addEventListener('click', addRow);
  addRow();  // first drawing row on page load

  uploadForm.addEventListener('submit', (e) => {
    if (designInput && designFiles.length === 0) {
      const ok = window.confirm(
        'No design input file added.\n\n' +
        'Without it, only title-block format and schedule arithmetic can be checked — ' +
        'reinforcement schedule comparisons will be skipped.\n\n' +
        'Continue without design input?'
      );
      if (!ok) {
        e.preventDefault();
        designDropZone.scrollIntoView({ behavior: 'smooth', block: 'center' });
        designDropZone.classList.remove('attention');
        void designDropZone.offsetWidth;
        designDropZone.classList.add('attention');
        return;
      }
    }
    const needsCompress = rows.some((r) => r.dxfFile && !r.dxfReady);
    if (needsCompress) {
      e.preventDefault();
      const label = rows.length > 1 ? `Reviewing ${rows.length} drawings...` : 'Reviewing drawing...';
      (async () => {
        startSubmitUI('Compressing DXF...');
        for (const r of rows) {
          if (r.dxfFile && !r.dxfReady) {
            const outFile = await gzipFile(r.dxfFile);
            const dt = new DataTransfer();
            dt.items.add(outFile);
            r.el.querySelector('.js-dxf-input').files = dt.files;
            r.dxfReady = true;
          }
        }
        startSubmitUI(label);
        uploadForm.submit();  // bypasses this listener — no recursion
      })();
      return;
    }
    startSubmitUI(rows.length > 1 ? `Reviewing ${rows.length} drawings...` : 'Reviewing drawing...');
  });
}

// ── Design inputs slot — shared by Mode A and Mode B; absent on reupload.html ─
const designInput    = document.getElementById('design-input');
const designList     = document.getElementById('design-file-list');
const designDropZone = document.getElementById('design-drop-zone');
let designFiles = [];

function renderDesignList() {
  designList.innerHTML = '';
  designFiles.forEach((f, i) => {
    const ext = f.name.split('.').pop().toUpperCase();
    const li = document.createElement('li');
    li.innerHTML = `
      <div class="file-row">
        <span class="file-row-icon">${ext}</span>
        <span class="file-row-name">${f.name}</span>
        <span class="file-row-meta">${formatSize(f.size)}</span>
        <button type="button" class="file-row-remove" data-idx="${i}" title="Remove">✕</button>
      </div>`;
    designList.appendChild(li);
  });
  syncDesignInput();
}

function syncDesignInput() {
  const dt = new DataTransfer();
  designFiles.forEach(f => dt.items.add(f));
  designInput.files = dt.files;
}

function addDesignFiles(files) {
  const allowed = ['.xlsx','.xls','.pdf','.jpg','.jpeg','.png'];
  Array.from(files).forEach(f => {
    const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
    if (allowed.includes(ext)) designFiles.push(f);
  });
  renderDesignList();
}

if (designInput) {
  designList.addEventListener('click', (e) => {
    const btn = e.target.closest('.file-row-remove');
    if (btn) {
      designFiles.splice(parseInt(btn.dataset.idx), 1);
      renderDesignList();
    }
  });

  designInput.addEventListener('change', () => { addDesignFiles(designInput.files); });

  designDropZone.addEventListener('click', (e) => { if (!e.target.closest('button, li')) designInput.click(); });
  designDropZone.addEventListener('dragover',  (e) => { e.preventDefault(); designDropZone.classList.add('dragover'); });
  designDropZone.addEventListener('dragleave', ()  => designDropZone.classList.remove('dragover'));
  designDropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    designDropZone.classList.remove('dragover');
    addDesignFiles(e.dataTransfer.files);
  });
}
