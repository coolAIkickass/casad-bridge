// ── Slot 1: Drawing PDF ────────────────────────────────────────────────────
// NOTE: this script is shared by upload.html AND reupload.html. The reupload
// page has no design-input slot, no #selected-size, no #drawing_name and no
// #review-wait-note — every reference to those must be null-guarded.
const dropZone     = document.getElementById('drop-zone');
const fileInput    = document.getElementById('file-input');
const dropLabel    = document.getElementById('drop-label');
const dropSelected = document.getElementById('drop-selected');
const filenameTxt  = document.getElementById('selected-filename');
const submitBtn    = document.getElementById('submit-btn');
const submitBtnDefaultText = submitBtn.textContent.trim();

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

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

  // Auto-set hidden drawing name from filename — strip extension, clean separators, cap 60 chars
  const nameInput = document.getElementById('drawing_name');
  if (nameInput) {
    const raw = file.name.replace(/\.pdf$/i, '').replace(/[_\-]+/g, ' ').trim();
    nameInput.value = raw.length > 60 ? raw.slice(0, 57) + '…' : raw;
  }

  // Draw attention to the DXF slot (next logical step after PDF)
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

fileInput.addEventListener('change', () => { if (fileInput.files[0]) setFile(fileInput.files[0]); });

dropZone.addEventListener('click', (e) => { if (!e.target.closest('button')) fileInput.click(); });
dropZone.addEventListener('dragover',  (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', ()  => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
});

// ── Slot 2: AutoCAD DXF (single file) ─────────────────────────────────────
const dxfInput    = document.getElementById('dxf-input');
const dxfList     = document.getElementById('dxf-file-list');
const dxfDropZone = document.getElementById('dxf-drop-zone');
let dxfFile  = null;
let dxfReady = false;  // true once dxfInput.files holds the final (gzipped) file for submit

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

// ── Slot 3: Design inputs (multiple) ──────────────────────────────────────
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

// ── DXF compression + submit ──────────────────────────────────────────────
// DXF is ASCII text — gzip cuts a 25 MB file to ~6 MB, so the upload fits
// inside Render's proxy window on slow office connections. The server
// accepts ".dxf.gz" and gunzips (_read_dxf_upload in ed_blueprint.py).
// This also fixes drag-and-drop: dxfFile is injected into dxfInput.files
// at submit time, where previously dropped files were never synced at all.
const uploadForm = document.getElementById('upload-form');

function startSubmitUI() {
  submitBtn.disabled = true;
  submitBtn.textContent = 'Reviewing drawing...';
  submitBtn.classList.add('reviewing');
  const waitNote = document.getElementById('review-wait-note');
  if (waitNote) waitNote.style.display = '';
}

async function prepareDxfAndSubmit() {
  startSubmitUI();
  let outFile = dxfFile;
  try {
    if (typeof CompressionStream !== 'undefined') {
      submitBtn.textContent = 'Compressing DXF...';
      const gzStream = dxfFile.stream().pipeThrough(new CompressionStream('gzip'));
      const gzBlob   = await new Response(gzStream).blob();
      outFile = new File([gzBlob], dxfFile.name + '.gz', { type: 'application/gzip' });
    }
  } catch (err) {
    outFile = dxfFile;  // compression failed — upload the raw DXF
  }
  const dt = new DataTransfer();
  dt.items.add(outFile);
  dxfInput.files = dt.files;
  dxfReady = true;
  submitBtn.textContent = 'Reviewing drawing...';
  uploadForm.submit();  // bypasses the submit handler — no recursion
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
    prepareDxfAndSubmit();
    return;
  }
  startSubmitUI();
});
