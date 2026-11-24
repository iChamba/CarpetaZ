export function lastUpdateText(isoDate, now = new Date()) {
  if (!isoDate) return "";
  const finished = new Date(isoDate);
  if (Number.isNaN(finished.getTime())) return "";
  const minutes = Math.max(0, Math.floor((now - finished) / 60000));
  if (minutes < 1) return "actualizado hace menos de 1 min";
  if (minutes < 60) return `actualizado hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `actualizado hace ${hours} h ${minutes % 60} min`;
}

export function setMessage(id, text = "", kind = "") {
  const target = document.getElementById(id);
  target.textContent = text;
  target.className = `notice${kind ? ` ${kind}` : ""}`;
}

export async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    if (!response.ok) throw new Error(`Error HTTP ${response.status}`);
    return response;
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Error HTTP ${response.status}`);
  return data;
}

export function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function makeButton(label, action, className = "button secondary small") {
  const button = element("button", label, className);
  button.type = "button";
  button.dataset.action = action;
  return button;
}

export function initDirectoryToggle(scope, label) {
  const toggle = document.getElementById(`${scope}-directory-toggle`);
  const content = document.getElementById(`${scope}-directory-content`);
  const layout = document.getElementById(`${scope}-layout`);
  toggle.addEventListener("click", () => {
    const collapsed = toggle.getAttribute("aria-expanded") === "true";
    content.hidden = collapsed;
    layout.classList.toggle("user-directory-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", `${collapsed ? "Mostrar" : "Ocultar"} ${label}`);
    toggle.textContent = collapsed ? `› Mostrar ${label}` : "‹ Ocultar";
  });
}

let pdfViewer;

function getPdfViewer() {
  if (pdfViewer) return pdfViewer;

  const dialog = element("dialog", undefined, "pdf-viewer-dialog");
  dialog.setAttribute("aria-labelledby", "pdf-viewer-title");
  const modal = element("div", undefined, "pdf-viewer-modal");
  const header = element("div", undefined, "pdf-viewer-modal-header");
  const title = element("h2", "", "pdf-viewer-modal-title");
  title.id = "pdf-viewer-title";
  const close = makeButton("Cerrar", "close-pdf-viewer", "button secondary");
  close.setAttribute("aria-label", "Cerrar visor ampliado");
  const frame = document.createElement("iframe");
  frame.title = "Visor ampliado de PDF";

  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => frame.removeAttribute("src"));

  header.append(title, close);
  modal.append(header, frame);
  dialog.append(modal);
  document.body.append(dialog);
  pdfViewer = { dialog, title, frame };
  return pdfViewer;
}

export function resetPreviewActions(title) {
  let actions = title.parentElement.querySelector(".preview-actions");
  if (!actions) {
    actions = element("div", undefined, "preview-actions");
    title.parentElement.insertBefore(actions, title);
  }
  actions.replaceChildren();
}

export function setPreviewCloseAction(title, closePreview) {
  const close = makeButton("Cerrar archivo", "close-preview", "button danger small");
  close.addEventListener("click", closePreview);
  title.parentElement.querySelector(".preview-actions").append(close);
}

export function setPdfPreviewActions(title, frame, closePreview) {
  setPreviewCloseAction(title, closePreview);
  const actions = title.parentElement.querySelector(".preview-actions");
  const enlarge = makeButton("Ampliar", "enlarge-preview", "button primary small");
  enlarge.addEventListener("click", () => {
    const viewer = getPdfViewer();
    viewer.title.textContent = title.textContent;
    viewer.frame.src = frame.src;
    if (!viewer.dialog.open) viewer.dialog.showModal();
  });
  actions.append(enlarge);
}

export function appendYearGroups(body, files, colspan, buildRow) {
  const groups = new Map();
  for (const file of files) {
    const year = file.created && !file.created.startsWith("1970") ? file.created.slice(0, 4) : "Sin fecha";
    if (!groups.has(year)) groups.set(year, []);
    groups.get(year).push(file);
  }
  let first = true;
  for (const [year, items] of groups) {
    const header = document.createElement("tr");
    header.className = "year-row";
    const cell = element("td", undefined, "year-cell");
    cell.colSpan = colspan;
    const label = element("button", undefined, "year-toggle");
    label.type = "button";
    const rows = items.map(buildRow);
    const setExpanded = expanded => {
      label.textContent = `${expanded ? "▾" : "▸"} ${year} (${items.length})`;
      label.setAttribute("aria-expanded", String(expanded));
      rows.forEach(row => row.classList.toggle("hidden", !expanded));
    };
    label.addEventListener("click", () => setExpanded(label.getAttribute("aria-expanded") !== "true"));
    setExpanded(first);
    first = false;
    cell.append(label);
    header.append(cell);
    body.append(header, ...rows);
  }
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
