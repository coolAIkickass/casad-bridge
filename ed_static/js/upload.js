// ── Slot 1: Drawing PDF ────────────────────────────────────────────────────
const dropZone     = document.getElementById('drop-zone');
const fileInput    = document.getElementById('file-input');
const dropLabel    = document.getElementById('drop-label');
const dropSelected = document.getElementById('drop-selected');
const filenameTxt  = document.getElementById('selected-filename');
const submitBtn    = document.getElementById('submit-btn');

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
  document.getElementById('selected-size').textContent = formatSize(file.size);
  dropLabel.style.display    = 'none';
  dropSelected.style.display = '';
  submitBtn.disabled = false;

  // Auto-set hidden drawing name from filename — strip extension, clean separators, cap 60 chars
  const nameInput = document.getElementById('drawing_name');
  const raw = file.name.replace(/\.pdf$/i, '').replace(/[_\-]+/g, ' ').trim();
  nameInput.value = raw.length > 60 ? raw.slice(0, 57) + '…' : raw;

  // Draw attention to the design input zone
  designDropZone.classList.remove('attention');
  void designDropZone.offsetWidth;  // force reflow to restart animation
  designDropZone.classList.add('attention');
  designDropZone.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  designDropZone.addEventListener('animationend', () => {
    designDropZone.classList.remove('attention');
  }, { once: true });
}

function clearFile() {
  fileInput.value = '';
  dropLabel.style.display    = '';
  dropSelected.style.display = 'none';
  submitBtn.disabled = true;
  submitBtn.textContent = 'Select a drawing to continue';
  document.getElementById('drawing_name').value = '';
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

// ── Slot 2: Design inputs (multiple) ──────────────────────────────────────
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

// ── Submit feedback ────────────────────────────────────────────────────────
document.getElementById('upload-form').addEventListener('submit', (e) => {
  if (designFiles.length === 0) {
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
  submitBtn.disabled = true;
  submitBtn.textContent = 'Reviewing drawing...';
  submitBtn.classList.add('reviewing');
  document.getElementById('review-wait-note').style.display = '';
});
