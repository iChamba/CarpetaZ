import { previewFile, extractRecord, revealFile } from "./documents.js";
import { makeButton, element, requestJson, setMessage } from "./common.js";

export function initSearch() {
  document.getElementById("pdf-search-form").addEventListener("submit", searchPdfs);
  document.getElementById("search-rows").addEventListener("click", onSearchAction);
  document.getElementById("rebuild-index").addEventListener("click", rebuildIndex);
}

async function searchPdfs(event) {
  event.preventDefault();
  const query = document.getElementById("pdf-query").value.trim();
  setMessage("search-message", "Buscando en el índice…");
  const body = document.getElementById("search-rows");
  body.replaceChildren();
  try {
    const data = await requestJson(`/api/search?q=${encodeURIComponent(query)}`);
    for (const result of data.items) {
      const row = document.createElement("tr");
      const pathCell = element("td", result.path, "file-name");
      const excerptCell = element("td", result.excerpt || "Sin fragmento disponible.", "excerpt");
      const actionsCell = document.createElement("td");
      const actions = element("div", undefined, "actions");
      const preview = makeButton("Ver PDF", "preview");
      preview.dataset.path = result.path;
      actions.append(preview);
      const reveal = makeButton("Ver en carpeta", "reveal");
      reveal.dataset.path = result.path;
      actions.append(reveal);
      const extract = makeButton("Extraer campos", "extract");
      extract.dataset.path = result.path;
      actions.append(extract);
      actionsCell.append(actions);
      row.append(pathCell, excerptCell, actionsCell);
      body.append(row);
    }
    const partial = data.index.running
      ? " El índice aún se está actualizando; estos resultados pueden ser parciales."
      : "";
    const noText = data.index.without_text
      ? ` ${data.index.without_text} PDF(s) no tienen texto extraíble.`
      : "";
    const more = data.items.length === 100 ? " Se muestran hasta 100 resultados." : "";
    setMessage("search-message", `${data.items.length} resultado(s).${partial}${noText}${more}`);
  } catch (error) {
    setMessage("search-message", error.message, "error");
  }
}

async function onSearchAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "preview") {
    previewFile(button.dataset.path, "search");
  }
  if (button.dataset.action === "reveal") {
    await revealFile(button.dataset.path, "search-message");
  }
  if (button.dataset.action === "extract") await extractRecord(button.dataset.path);
}

async function rebuildIndex() {
  try {
    await requestJson("/api/index", { method: "POST" });
    setMessage("search-message", "Se inició la actualización del índice.");
    refreshIndexStatus();
  } catch (error) {
    setMessage("search-message", error.message, "error");
  }
}

export async function refreshIndexStatus() {
  try {
    const status = await requestJson("/api/status");
    const errorDetail = status.error_details && status.error_details.length
      ? ` · ${status.error_details[0]}`
      : "";
    const label = status.running
      ? `${status.message}: ${status.processed}/${status.total}`
      : `${status.message} · ${status.total} PDF(s), ${status.errors} error(es)${errorDetail}`;
    document.getElementById("index-status").textContent = label;
    if (status.running) window.setTimeout(refreshIndexStatus, 2000);
  } catch (error) {
    document.getElementById("index-status").textContent =
      `No se pudo consultar el índice: ${error.message}`;
  }
}
