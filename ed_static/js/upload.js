// ── Slot 1: Drawing PDF ────────────────────────────────────────────────────
const dropZone     = document.getElementById('drop-zone');
const fileInput    = document.getElementById('file-input');
const dropLabel    = document.getElementById('drop-label');
const dropSelected = document.getElementById('drop-selected');
const filenameTxt  = document.getElementById('selected-filename');
const submitBtn    = document.getElementById('submit-btn');

function setFile(file) {
  if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
    alert('Please select a PDF file for the drawing.');
    return;
  }
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
  filenameTxt.textContent    = file.name;
  dropLabel.style.display    = 'none';
  dropSelected.style.display = '';
  submitBtn.disabled = false;
}

function clearFile() {
  fileInput.value = '';
  dropLabel.style.display    = '';
  dropSelected.style.display = 'none';
  submitBtn.disabled = true;
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
    const li = document.createElement('li');
    li.className = 'di-file-item';
    li.innerHTML = `<span class="di-file-name">${f.name}</span>
      <button type="button" class="link-btn di-remove" data-idx="${i}">Remove</button>`;
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
  const btn = e.target.closest('.di-remove');
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
document.getElementById('upload-form').addEventListener('submit', () => {
  submitBtn.disabled = true;
  submitBtn.textContent = 'Analysing drawing...';
});
