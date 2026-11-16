import { element, requestJson, setMessage } from "./common.js";
import { revealFile } from "./documents.js";

const PAGE_SIZE = 50;
let workbookPath = "";
let workbookSheet = "";
let workbookStart = 1;
let workbookMaxRow = 0;
let workbookVersion = "";
let pendingCells = new Map();

export function initWorkbooks() {
  document.getElementById("workbook-select").addEventListener("change", selectWorkbook);
  document.getElementById("sheet-select").addEventListener("change", selectSheet);
  document.getElementById("workbook-reload").addEventListener("click", reloadWorkbook);
  document.getElementById("workbook-reveal").addEventListener("click", () => {
    if (workbookPath) revealFile(workbookPath, "workbook-message");
  });
  document.getElementById("workbook-prev").addEventListener("click", previousPage);
  document.getElementById("workbook-next").addEventListener("click", nextPage);
  document.getElementById("workbook-save").addEventListener("click", saveWorkbook);
}

export async function loadWorkbooks() {
  const select = document.getElementById("workbook-select");
  if (select.dataset.loaded === "true") return true;
  select.replaceChildren(new Option("Selecciona un libro…", ""));
  try {
    const data = await requestJson("/api/workbooks");
    for (const path of data.items) select.add(new Option(path, path));
    select.dataset.loaded = "true";
    if (!data.items.length) {
      setMessage("workbook-message", "No hay archivos .xlsx en la carpeta configurada.");
    }
    return true;
  } catch (error) {
    setMessage("workbook-message", error.message, "error");
    return false;
  }
}

async function selectWorkbook(event) {
  if (pendingCells.size && !window.confirm("Se descartarán los cambios sin guardar. ¿Continuar?")) {
    event.target.value = workbookPath;
    return;
  }
  workbookPath = event.target.value;
  workbookSheet = "";
  workbookStart = 1;
  pendingCells.clear();
  const sheetSelect = document.getElementById("sheet-select");
  sheetSelect.replaceChildren();
  sheetSelect.disabled = !workbookPath;
  document.getElementById("workbook-save").disabled = true;
  document.getElementById("workbook-reload").disabled = !workbookPath;
  document.getElementById("workbook-reveal").disabled = !workbookPath;
  document.getElementById("workbook-grid").replaceChildren();
  if (workbookPath) await loadWorksheet(true);
}

async function selectSheet(event) {
  if (pendingCells.size && !window.confirm("Se descartarán los cambios sin guardar. ¿Continuar?")) {
    event.target.value = workbookSheet;
    return;
  }
  workbookSheet = event.target.value;
  workbookStart = 1;
  pendingCells.clear();
  await loadWorksheet(true);
}

async function reloadWorkbook() {
  if (pendingCells.size && !window.confirm("Se descartarán los cambios sin guardar. ¿Recargar el libro?")) return;
  pendingCells.clear();
  await loadWorksheet(false);
}

async function loadWorksheet(resetSheets) {
  if (!workbookPath) return;
  const params = new URLSearchParams({
    path: workbookPath,
    sheet: workbookSheet,
    start: String(workbookStart),
    rows: String(PAGE_SIZE),
  });
  setMessage("workbook-message", "Cargando hoja…");
  try {
    const data = await requestJson(`/api/workbook?${params}`);
    workbookSheet = data.sheet;
    workbookVersion = data.version;
    workbookMaxRow = data.max_row;
    if (resetSheets) {
      const select = document.getElementById("sheet-select");
      select.replaceChildren(...data.sheets.map(name => new Option(name, name)));
      select.value = data.sheet;
      select.disabled = false;
    }
    renderWorksheet(data);
    document.getElementById("workbook-save").disabled = pendingCells.size === 0;
    const lastRow = Math.min(workbookMaxRow, workbookStart + data.rows.length - 1);
    document.getElementById("workbook-page-label").textContent =
      `Filas ${data.rows.length ? `${workbookStart}–${lastRow}` : "0"} de ${workbookMaxRow}`;
    document.getElementById("workbook-prev").disabled = workbookStart <= 1;
    document.getElementById("workbook-next").disabled = workbookStart + PAGE_SIZE > workbookMaxRow;
    const note = data.truncated_columns
      ? "La hoja tiene más de 50 columnas; solo se muestran las primeras 50."
      : "";
    setMessage("workbook-message", note);
  } catch (error) {
    setMessage("workbook-message", error.message, "error");
  }
}

function renderWorksheet(data) {
  const table = document.getElementById("workbook-grid");
  table.replaceChildren();
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  headerRow.append(element("th", "Fila"));
  for (let column = 1; column <= data.max_column; column++) {
    headerRow.append(element("th", columnLabel(column)));
  }
  head.append(headerRow);
  table.append(head);

  const body = document.createElement("tbody");
  data.rows.forEach((cells, rowIndex) => {
    const absoluteRow = data.start + rowIndex;
    const row = document.createElement("tr");
    row.append(element("th", String(absoluteRow)));
    cells.forEach((cell, columnIndex) => {
      const cellElement = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.value = cell.value === null ? "" : String(cell.value);
      input.dataset.row = String(absoluteRow);
      input.dataset.column = String(columnIndex + 1);
      if (cell.formula) {
        input.readOnly = true;
        input.title = "Las celdas con fórmula no se pueden editar desde esta vista.";
      }
      input.addEventListener("input", () => {
        pendingCells.set(`${absoluteRow}:${columnIndex + 1}`, {
          row: absoluteRow,
          column: columnIndex + 1,
          value: input.value,
        });
        document.getElementById("workbook-save").disabled = false;
        setMessage("workbook-message", `${pendingCells.size} celda(s) sin guardar.`);
      });
      cellElement.append(input);
      row.append(cellElement);
    });
    body.append(row);
  });
  table.append(body);
}

function columnLabel(number) {
  let label = "";
  while (number > 0) {
    const remainder = (number - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    number = Math.floor((number - 1) / 26);
  }
  return label;
}

async function previousPage() {
  if (pendingCells.size && !window.confirm("Se descartarán los cambios sin guardar. ¿Continuar?")) return;
  pendingCells.clear();
  workbookStart = Math.max(1, workbookStart - PAGE_SIZE);
  await loadWorksheet(false);
}

async function nextPage() {
  if (pendingCells.size && !window.confirm("Se descartarán los cambios sin guardar. ¿Continuar?")) return;
  pendingCells.clear();
  workbookStart += PAGE_SIZE;
  await loadWorksheet(false);
}

async function saveWorkbook() {
  if (!pendingCells.size) return;
  const button = document.getElementById("workbook-save");
  button.disabled = true;
  try {
    const result = await requestJson("/api/workbook/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: workbookPath,
        sheet: workbookSheet,
        version: workbookVersion,
        updates: [...pendingCells.values()],
      }),
    });
    workbookVersion = result.version;
    pendingCells.clear();
    await loadWorksheet(false);
    setMessage("workbook-message", "Cambios guardados. Se creó una copia de respaldo.", "success");
  } catch (error) {
    setMessage("workbook-message", error.message, "error");
    button.disabled = pendingCells.size === 0;
  }
}
