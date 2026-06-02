const dropZone    = document.getElementById('drop-zone');
const fileInput   = document.getElementById('file-input');
const dropLabel   = document.getElementById('drop-label');
const dropSelected = document.getElementById('drop-selected');
const filenameTxt = document.getElementById('selected-filename');
const submitBtn   = document.getElementById('submit-btn');

function setFile(file) {
  if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
    alert('Please select a PDF file.');
    return;
  }
  // Transfer to the real input via DataTransfer
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;

  filenameTxt.textContent = file.name;
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

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) setFile(fileInput.files[0]);
});

dropZone.addEventListener('click', (e) => {
  if (!e.target.closest('button')) fileInput.click();
});

dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});
