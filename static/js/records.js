import { compareAuthorizationRecency } from "./users.js";
import { element, makeButton, requestJson, setMessage } from "./common.js";
import { revealFolder } from "./documents.js";
import { workflowDeadline, workflowStatusGroup } from "./workflows.js";

const RECORDS_PAGE_SIZE = 100;
const LICENSE_STATUSES = [
  { key: "valid", label: "Vigentes", className: "is-valid", detail: "VIGENTE" },
  { key: "soon", label: "Próximas a vencer", className: "is-soon", detail: "PRÓXIMO A VENCERSE" },
  { key: "expired", label: "Vencidas", className: "is-expired", detail: "VENCIDA" },
  { key: "unknown", label: "Sin fecha", className: "is-unknown", detail: "SIN FECHA DE VENCIMIENTO" },
];
let recordsOffset = 0;
let recordsTotal = 0;
let licenseOffset = 0;
let licenseTotal = 0;
let licenseLoadGeneration = 0;
let workflowRecordsGeneration = 0;
const WORKFLOW_RECORD_GROUPS = [
  { key: "on-time", label: "En curso · En plazo", className: "is-valid" },
  { key: "warning", label: "En curso · Por vencer", className: "is-soon" },
  { key: "overdue", label: "En curso · Vencidos", className: "is-expired" },
  { key: "unavailable", label: "En curso · Plazo no disponible", className: "is-unknown" },
  { key: "observed", label: "Observados", className: "is-pending" },
  { key: "rejected", label: "Rechazados", className: "is-expired" },
  { key: "completed", label: "Finalizados", className: "is-valid" },
  { key: "delivered", label: "Entregados", className: "is-valid" },
];

export function workflowRecordsSummary(cases, supportsDeadline, now = Date.now()) {
  const counts = Object.fromEntries(WORKFLOW_RECORD_GROUPS.map(group => [group.key, 0]));
  for (const item of cases) {
    let group = workflowStatusGroup(item);
    if (group === "active") {
      group = supportsDeadline ? workflowDeadline(item, now).tone : "unavailable";
    }
    counts[group] += 1;
  }
  return counts;
}

export function initRecords() {
  document.getElementById("record-rows").addEventListener("click", handleRecordAction);
  document.getElementById("records-refresh").addEventListener("click", () => loadRecords(true));
  document.getElementById("records-prev").addEventListener("click", () => {
    loadUserRecords(false, Math.max(0, recordsOffset - RECORDS_PAGE_SIZE));
  });
  document.getElementById("records-next").addEventListener("click", () => {
    loadUserRecords(false, recordsOffset + RECORDS_PAGE_SIZE);
  });
  document.getElementById("license-records-prev").addEventListener("click", () => {
    loadLicenseRecords(Math.max(0, licenseOffset - RECORDS_PAGE_SIZE));
  });
  document.getElementById("license-records-next").addEventListener("click", () => {
    loadLicenseRecords(licenseOffset + RECORDS_PAGE_SIZE);
  });
  window.setInterval(() => {
    if (document.getElementById("pane-records").classList.contains("active")) {
      loadWorkflowRecords();
    }
  }, 30000);
}

export async function loadRecords(forceRefresh = false) {
  const results = await Promise.all([
    loadUserRecords(forceRefresh),
    loadLicenseRecords(forceRefresh ? 0 : licenseOffset),
    loadWorkflowRecords(),
  ]);
  return results.every(Boolean);
}

export async function loadWorkflowRecords() {
  const generation = ++workflowRecordsGeneration;
  const container = document.getElementById("workflow-records-groups");
  try {
    const data = await requestJson("/api/workflow");
    if (generation !== workflowRecordsGeneration) return false;
    const supported = data.supports_observation_deadline === true;
    renderRecordsSummary("workflow-records-groups",
      workflowRecordsSummary(data.cases, supported), WORKFLOW_RECORD_GROUPS);
    setMessage("workflow-records-message", supported ? "" :
      "Reinicia la aplicación para calcular el estado temporal de los trámites.");
    return true;
  } catch (error) {
    if (generation === workflowRecordsGeneration) {
      container.replaceChildren();
      setMessage("workflow-records-message", `No se pudieron cargar los trámites: ${error.message}`, "error");
    }
    return false;
  }
}

async function loadUserRecords(forceRefresh = false, offset = recordsOffset) {
  const body = document.getElementById("record-rows");
  body.replaceChildren();
  const summary = document.getElementById("records-summary");
  summary.replaceChildren();
  try {
    const params = new URLSearchParams({
      offset: String(forceRefresh ? 0 : offset),
      limit: String(RECORDS_PAGE_SIZE),
    });
    const data = await requestJson(`/api/users?${params}`);
    const users = data.items;
    recordsOffset = data.offset;
    recordsTotal = data.total;
    renderRecordsSummary("records-summary", data.summary, [
      ...LICENSE_STATUSES,
      { key: "pending", label: "Pendientes de importar", className: "is-pending" },
      { key: "none", label: "Sin autorización", className: "is-empty" },
    ]);
    for (const user of users) {
      const latestRecord = [...(user.authorization_records || [])]
        .sort(compareAuthorizationRecency)[0];
      const row = document.createElement("tr");
      row.append(element("td", user.name));

      if (!latestRecord) {
        const folderCell = document.createElement("td");
        folderCell.colSpan = 8;
        const folderButton = makeButton("Ver carpeta", "open-folder");
        folderButton.dataset.name = user.name;
        folderCell.append(folderButton);
        row.append(folderCell);
      } else {
        const sourceCell = document.createElement("td");
        const viewLink = document.createElement("a");
        viewLink.href = `/api/preview?path=${encodeURIComponent(latestRecord.source_path)}`;
        viewLink.target = "_blank";
        viewLink.rel = "noopener noreferrer";
        viewLink.className = "button secondary small";
        viewLink.textContent = "Ver archivo";
        sourceCell.append(viewLink);
        row.append(sourceCell);
        for (const field of [
          "first_name", "identity_number", "authorization_number", "practice",
          "issue_date", "expiry_date", "equipment",
        ]) {
          row.append(element("td", latestRecord[field] || ""));
        }
      }

      body.append(row);
    }
    const first = recordsTotal ? recordsOffset + 1 : 0;
    const last = Math.min(recordsOffset + users.length, recordsTotal);
    document.getElementById("records-page-label").textContent =
      `${first}–${last} de ${recordsTotal} usuarios`;
    document.getElementById("records-prev").disabled = recordsOffset <= 0;
    document.getElementById("records-next").disabled =
      recordsOffset + users.length >= recordsTotal;
    if (!recordsTotal) {
      setMessage("records-message", "No se encontraron carpetas de usuarios.");
    }
    return true;
  } catch (error) {
    setMessage("records-message", error.message, "error");
    return false;
  }
}

async function loadLicenseRecords(offset = licenseOffset) {
  const generation = ++licenseLoadGeneration;
  const container = document.getElementById("license-records");
  const previous = document.getElementById("license-records-prev");
  const next = document.getElementById("license-records-next");
  previous.disabled = true;
  next.disabled = true;
  setMessage("license-records-message", "Cargando licencias…");
  try {
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(RECORDS_PAGE_SIZE),
    });
    const data = await requestJson(`/api/entity/licenses?${params}`);
    if (generation !== licenseLoadGeneration) return false;
    licenseOffset = data.offset;
    licenseTotal = data.total;
    renderRecordsSummary("license-records-summary", data.summary, LICENSE_STATUSES);
    container.replaceChildren();
    let practice;
    let body;
    for (const license of data.items) {
      if (license.practice !== practice) {
        practice = license.practice;
        const section = element("section", undefined, "license-records-practice");
        section.append(element("h4", practice));
        const wrap = element("div", undefined, "table-wrap");
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const header = document.createElement("tr");
        [
          "Institución", "Licencia", "Representante legal", "N.º autorización",
          "Expedición", "Vencimiento", "Estado", "Documentos",
        ].forEach(label => header.append(element("th", label)));
        head.append(header);
        body = document.createElement("tbody");
        table.append(head, body);
        wrap.append(table);
        section.append(wrap);
        container.append(section);
      }
      const row = document.createElement("tr");
      for (const field of [
        "institution", "license_number", "representative",
        "authorization_number", "issue_date", "expiry_date",
      ]) {
        row.append(element("td", license[field] || "Sin dato"));
      }
      const status = LICENSE_STATUSES.find(item => item.key === license.status);
      const statusCell = document.createElement("td");
      statusCell.append(element("span", status.detail, `authorization-status ${status.className}`));
      row.append(statusCell);
      const sources = document.createElement("td");
      for (const documentSource of license.documents) {
        const link = element("a", documentSource.name);
        link.href = `/api/entity/preview?path=${encodeURIComponent(documentSource.path)}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        sources.append(link, document.createElement("br"));
      }
      row.append(sources);
      body.append(row);
    }
    const first = licenseTotal ? licenseOffset + 1 : 0;
    const last = Math.min(licenseOffset + data.items.length, licenseTotal);
    document.getElementById("license-records-page-label").textContent =
      `${first}–${last} de ${licenseTotal} licencias`;
    previous.disabled = licenseOffset <= 0;
    next.disabled = licenseOffset + data.items.length >= licenseTotal;
    if (data.warnings) {
      setMessage("license-records-message", `Se omitieron ${data.warnings} ubicaciones inaccesibles.`, "error");
    } else if (!data.catalog.ready) {
      setMessage("license-records-message", data.catalog.message);
    } else {
      setMessage("license-records-message", licenseTotal ? "" : "No hay licencias procesadas.");
    }
    return true;
  } catch (error) {
    if (generation === licenseLoadGeneration) {
      container.replaceChildren();
      document.getElementById("license-records-summary").replaceChildren();
      document.getElementById("license-records-page-label").textContent = "";
      setMessage("license-records-message", `No se pudieron cargar las licencias: ${error.message}`, "error");
    }
    return false;
  }
}

function renderRecordsSummary(id, counts, statuses) {
  const summary = document.getElementById(id);
  summary.replaceChildren();
  for (const status of statuses) {
    const card = element("div", undefined, `records-summary-card ${status.className}`);
    card.append(element("span", status.label, "records-summary-label"));
    card.append(element("strong", counts[status.key].toLocaleString("es"), "records-summary-count"));
    summary.append(card);
  }
}

async function handleRecordAction(event) {
  const folderButton = event.target.closest("button[data-action='open-folder']");
  if (folderButton) {
    await revealFolder(folderButton.dataset.name, "records-message");
  }
}
