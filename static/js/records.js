import {
  compareAuthorizationRecency,
  getLoadedUsers,
  getUserAuthorizationStatus,
  loadUsers,
} from "./users.js";
import { element, makeButton, requestJson, setMessage } from "./common.js";
import { revealFolder } from "./documents.js";

export function initRecords() {
  document.getElementById("record-rows").addEventListener("click", handleRecordAction);
  document.getElementById("records-refresh").addEventListener("click", () => loadRecords(true));
}

export async function loadRecords(forceRefresh = false) {
  const body = document.getElementById("record-rows");
  body.replaceChildren();
  const summary = document.getElementById("records-summary");
  summary.replaceChildren();
  try {
    let users = forceRefresh ? null : getLoadedUsers();
    if (!users && !forceRefresh && !document.getElementById("user-query").value.trim()) {
      if (!await loadUsers()) return false;
      users = getLoadedUsers();
    }
    if (!users) {
      users = (await requestJson("/api/users")).items;
      window.dispatchEvent(new Event("users-stale"));
    }
    renderRecordsSummary(users);
    for (const user of users) {
      const latestRecord = [...(user.authorization_records || [])]
        .sort(compareAuthorizationRecency)[0];
      const row = document.createElement("tr");
      row.append(element("td", user.name));

      if (!latestRecord) {
        const folderCell = document.createElement("td");
        folderCell.colSpan = 9;
        const folderButton = makeButton("Ver carpeta", "open-folder");
        folderButton.dataset.name = user.name;
        folderCell.append(folderButton);
        row.append(folderCell);
      } else {
        row.dataset.id = latestRecord.id;
        const sourceCell = document.createElement("td");
        const viewLink = document.createElement("a");
        viewLink.href = `/api/preview?path=${encodeURIComponent(latestRecord.source_path)}`;
        viewLink.target = "_blank";
        viewLink.rel = "noopener noreferrer";
        viewLink.className = "button secondary small";
        viewLink.textContent = "Ver archivo";
        sourceCell.append(viewLink);
        row.append(sourceCell);
        row.append(recordInput(latestRecord, "first_name", "Nombre"));
        row.append(recordInput(latestRecord, "identity_number", "Cédula"));
        row.append(recordInput(latestRecord, "authorization_number", "Número de autorización"));
        row.append(recordInput(latestRecord, "practice", "Práctica"));
        row.append(recordInput(latestRecord, "issue_date", "Fecha de expedición"));
        row.append(recordInput(latestRecord, "expiry_date", "Fecha de vencimiento"));
        row.append(recordInput(latestRecord, "equipment", "Equipos asociados", true));
        const saveCell = document.createElement("td");
        saveCell.append(makeButton("Guardar", "save-record", "button primary small"));
        row.append(saveCell);
      }

      body.append(row);
    }
    if (!users.length) {
      setMessage("records-message", "No se encontraron carpetas de usuarios.");
    }
    return true;
  } catch (error) {
    setMessage("records-message", error.message, "error");
    return false;
  }
}

function renderRecordsSummary(users) {
  const counts = {
    valid: 0,
    soon: 0,
    expired: 0,
    unknown: 0,
    pending: 0,
    none: 0,
  };
  for (const user of users) {
    counts[getUserAuthorizationStatus(user).filterValue] += 1;
  }

  const summary = document.getElementById("records-summary");
  const statuses = [
    { key: "valid", label: "Vigentes", className: "is-valid" },
    { key: "soon", label: "Próximas a vencer", className: "is-soon" },
    { key: "expired", label: "Vencidas", className: "is-expired" },
    { key: "unknown", label: "Sin fecha", className: "is-unknown" },
    { key: "pending", label: "Pendientes de importar", className: "is-pending" },
    { key: "none", label: "Sin autorización", className: "is-empty" },
  ];
  for (const status of statuses) {
    const card = element("div", undefined, `records-summary-card ${status.className}`);
    card.append(element("span", status.label, "records-summary-label"));
    card.append(element("strong", counts[status.key].toLocaleString("es"), "records-summary-count"));
    summary.append(card);
  }
}

function recordInput(record, field, label, multiline = false) {
  const cell = document.createElement("td");
  const input = document.createElement(multiline ? "textarea" : "input");
  input.value = record[field] || "";
  input.setAttribute("aria-label", label);
  input.dataset.field = field;
  cell.append(input);
  return cell;
}

async function handleRecordAction(event) {
  const folderButton = event.target.closest("button[data-action='open-folder']");
  if (folderButton) {
    await revealFolder(folderButton.dataset.name, "records-message");
    return;
  }

  const button = event.target.closest("button[data-action='save-record']");
  if (!button) return;
  const row = button.closest("tr");
  const fields = {};
  row.querySelectorAll("[data-field]").forEach(input => {
    fields[input.dataset.field] = input.value;
  });
  button.disabled = true;
  try {
    await requestJson("/api/records/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: row.dataset.id, fields }),
    });
    window.dispatchEvent(new Event("users-stale"));
    await loadRecords();
    setMessage("records-message", "Registro guardado.", "success");
  } catch (error) {
    setMessage("records-message", error.message, "error");
  } finally {
    button.disabled = false;
  }
}
