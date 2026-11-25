// Frontend del convertitore: drag & drop, scelta del formato, upload e polling.

const $ = (sel) => document.querySelector(sel);

const dropzone = $("#dropzone");
const fileInput = $("#file-input");
const list = $("#file-list");
const toolbar = $("#toolbar");
const allTarget = $("#all-target");
const template = $("#card-template");

let formatMap = {};     // estensione -> [formati di uscita]
let categories = {};    // estensione -> immagine | video | audio | documento
let maxUploadMb = 500;
const items = [];       // un elemento per ogni file nella lista

// Formato proposto di default per ogni categoria (il primo disponibile)
const PREFERRED = {
  immagine: ["jpg", "png"],
  video: ["mp4", "mp3"],
  audio: ["mp3", "wav"],
  documento: ["pdf", "html"],
};

// ---------------------------------------------------------------------------
// Avvio
// ---------------------------------------------------------------------------

async function init() {
  const res = await fetch("/api/formats");
  const data = await res.json();
  formatMap = data.map;
  categories = data.categories;
  maxUploadMb = data.max_upload_mb;
  $("#limits").textContent =
    `Massimo ${maxUploadMb} MB per file · i file vengono cancellati dopo ${data.ttl_minutes} minuti`;
  renderFormatsTable();
}

function renderFormatsTable() {
  const groups = {};
  for (const [ext, cat] of Object.entries(categories)) {
    (groups[cat] ||= []).push(ext);
  }
  const box = document.createElement("div");
  box.className = "formats";
  for (const [cat, exts] of Object.entries(groups)) {
    const label = document.createElement("b");
    label.textContent = cat;
    const values = document.createElement("span");
    values.textContent = exts.map((e) => "." + e).join("  ");
    box.append(label, values);
  }
  $("#formats-table").append(box);
}

// ---------------------------------------------------------------------------
// Drag & drop
// ---------------------------------------------------------------------------

["dragenter", "dragover"].forEach((evt) =>
  document.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragging");
  })
);
["dragleave", "drop"].forEach((evt) =>
  document.addEventListener(evt, (e) => {
    e.preventDefault();
    if (evt === "drop" || e.relatedTarget === null) dropzone.classList.remove("dragging");
  })
);
document.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

// ---------------------------------------------------------------------------
// Gestione della lista
// ---------------------------------------------------------------------------

function extOf(name) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function addFiles(fileList) {
  for (const file of fileList) {
    const ext = extOf(file.name);
    const targets = formatMap[ext] || [];
    const category = categories[ext] || null;
    const preferred = (PREFERRED[category] || []).find((t) => targets.includes(t));
    const item = {
      file, ext, category, targets,
      target: preferred || targets[0] || "",
      state: "pronto", progress: null, jobId: null, error: null,
    };
    if (!targets.length) {
      item.state = "errore";
      item.error = ext ? `Formato .${ext} non supportato` : "File senza estensione";
    } else if (file.size > maxUploadMb * 1024 * 1024) {
      item.state = "errore";
      item.error = `Troppo grande (massimo ${maxUploadMb} MB)`;
    }
    item.el = createCard(item);
    items.push(item);
    list.append(item.el);
    updateCard(item);
  }
  refreshToolbar();
}

function createCard(item) {
  const el = template.content.firstElementChild.cloneNode(true);
  const badge = el.querySelector(".badge");
  badge.textContent = item.ext || "?";
  if (item.category) badge.classList.add(item.category);

  el.querySelector(".name").textContent = item.file.name; // textContent: niente HTML iniettato
  el.querySelector(".name").title = item.file.name;
  el.querySelector(".size").textContent = formatSize(item.file.size);

  const select = el.querySelector(".target");
  for (const t of item.targets) select.add(new Option("." + t, t));
  select.value = item.target;
  select.addEventListener("change", () => {
    item.target = select.value;
    if (item.state === "completato") item.state = "pronto"; // si può riconvertire
    updateCard(item);
  });

  el.querySelector(".convert-btn").addEventListener("click", () => startConversion(item));
  el.querySelector(".remove-btn").addEventListener("click", () => removeItem(item));
  return el;
}

const STATUS_TEXT = {
  "pronto": "",
  "caricamento": "Caricamento…",
  "in coda": "In coda…",
  "in corso": "Conversione in corso…",
  "completato": "Fatto",
};

function updateCard(item) {
  const el = item.el;
  const busy = ["caricamento", "in coda", "in corso"].includes(item.state);
  const select = el.querySelector(".target");
  const progress = el.querySelector(".progress");
  const bar = el.querySelector(".progress-bar");
  const download = el.querySelector(".download-btn");
  const convertBtn = el.querySelector(".convert-btn");

  el.classList.toggle("is-error", item.state === "errore");
  el.classList.toggle("is-done", item.state === "completato");

  select.hidden = !item.targets.length;
  el.querySelector(".arrow").hidden = !item.targets.length;
  select.disabled = busy;
  select.value = item.target;

  let text = item.state === "errore" ? item.error : STATUS_TEXT[item.state];
  if (busy && item.progress != null) text += ` ${Math.round(item.progress * 100)}%`;
  el.querySelector(".status").textContent = text;

  progress.classList.toggle("visible", busy);
  progress.classList.toggle("indeterminate", busy && item.progress == null);
  bar.style.width = item.progress != null ? `${item.progress * 100}%` : "";

  const canConvert = item.targets.length && !busy && item.file.size <= maxUploadMb * 1024 * 1024;
  convertBtn.hidden = !canConvert || item.state === "completato";
  convertBtn.textContent = item.state === "errore" ? "Riprova" : "Converti";

  download.hidden = item.state !== "completato";
  if (item.state === "completato") {
    download.href = `/api/jobs/${item.jobId}/download`;
    download.download = item.downloadName;
  }
  el.querySelector(".remove-btn").disabled = busy;
}

function removeItem(item) {
  if (item.jobId && ["completato", "errore"].includes(item.state)) {
    fetch(`/api/jobs/${item.jobId}`, { method: "DELETE" }).catch(() => {});
  }
  item.el.remove();
  items.splice(items.indexOf(item), 1);
  refreshToolbar();
}

function refreshToolbar() {
  toolbar.hidden = items.length === 0;
  // "Converti tutti in": solo i formati che almeno un file può raggiungere
  const all = [...new Set(items.flatMap((i) => i.targets))].sort();
  const current = allTarget.value;
  allTarget.length = 1;
  for (const t of all) allTarget.add(new Option("." + t, t));
  allTarget.value = all.includes(current) ? current : "";
}

allTarget.addEventListener("change", () => {
  const t = allTarget.value;
  if (!t) return;
  for (const item of items) {
    if (item.targets.includes(t) && !["caricamento", "in coda", "in corso"].includes(item.state)) {
      item.target = t;
      if (item.state === "completato") item.state = "pronto"; // nuova conversione possibile
      updateCard(item);
    }
  }
});

$("#convert-all-btn").addEventListener("click", () => {
  for (const item of items) {
    if (item.targets.length && ["pronto", "errore"].includes(item.state)) startConversion(item);
  }
});

$("#clear-btn").addEventListener("click", () => {
  for (const item of [...items]) {
    if (!["caricamento", "in coda", "in corso"].includes(item.state)) removeItem(item);
  }
});

// ---------------------------------------------------------------------------
// Upload e polling
// ---------------------------------------------------------------------------

function upload(item) {
  // XMLHttpRequest (e non fetch) per avere l'avanzamento dell'upload
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", item.file);
    form.append("target", item.target);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        item.progress = e.loaded / e.total;
        updateCard(item);
      }
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.detail || `Errore del server (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Connessione al server non riuscita"));
    xhr.send(form);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startConversion(item) {
  item.state = "caricamento";
  item.progress = 0;
  item.error = null;
  updateCard(item);

  try {
    const job = await upload(item);
    item.jobId = job.id;
    item.downloadName = job.download_name;

    while (true) {
      item.state = job.status;
      item.progress = job.progress;
      updateCard(item);
      if (job.status === "completato") break;
      if (job.status === "errore") throw new Error(job.error);
      await sleep(1000);
      const res = await fetch(`/api/jobs/${item.jobId}`);
      if (!res.ok) throw new Error("Conversione non più disponibile");
      Object.assign(job, await res.json());
    }
  } catch (err) {
    item.state = "errore";
    item.error = err.message;
  }
  item.progress = null;
  updateCard(item);
}

init().catch(() => {
  $("#limits").textContent = "Impossibile contattare il server";
});
