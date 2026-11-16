import { extractRecord, previewFile, revealFile } from "./documents.js";
import { element, formatSize, makeButton, requestJson, setMessage } from "./common.js";

let selectedUser = "";
let usersLoadGeneration = 0;
let usersLoadPromise = null;
let usersLoadQuery = null;
let loadedUsers = [];
let loadedUsersQuery = null;
let userListWarnings = 0;

export function initUsers() {
  document.getElementById("user-filter").addEventListener("submit", event => {
    event.preventDefault();
    loadUsers();
  });
  const authorizationFilter = document.getElementById("user-authorization-filter");
  const statusFilter = document.getElementById("user-status-filter");
  const practiceFilter = document.getElementById("user-practice-filter");
  authorizationFilter.addEventListener("change", () => {
    statusFilter.disabled = authorizationFilter.value === "without";
    practiceFilter.disabled = authorizationFilter.value === "without";
    if (statusFilter.disabled) statusFilter.value = "";
    if (practiceFilter.disabled) practiceFilter.value = "";
    renderUserList();
  });
  statusFilter.addEventListener("change", renderUserList);
  practiceFilter.addEventListener("input", renderUserList);
  document.getElementById("user-list").addEventListener("click", event => {
    const button = event.target.closest("button[data-user]");
    if (button) loadUser(button.dataset.user);
  });
  document.getElementById("user-profile").addEventListener("click", handleProfileAction);
  document.getElementById("import-certificates").addEventListener("click", startCertificateImport);
  refreshCertificateImportStatus();
}

async function startCertificateImport() {
  const button = document.getElementById("import-certificates");
  button.disabled = true;
  setMessage("user-message", "Iniciando revisión de archivos…");
  try {
    await requestJson("/api/certificates/import", { method: "POST" });
    await refreshCertificateImportStatus();
  } catch (error) {
    button.disabled = false;
    setMessage("user-message", error.message, "error");
  }
}

async function refreshCertificateImportStatus() {
  const button = document.getElementById("import-certificates");
  try {
    const status = await requestJson("/api/certificates/import/status");
    if (status.running) {
      button.disabled = true;
      setMessage(
        "user-message",
        `${status.message}: ${status.imported} nuevos, ${status.updated} actualizados, ` +
          `${status.without_text} sin texto, ${status.errors} errores.`,
      );
      window.setTimeout(refreshCertificateImportStatus, 1500);
      return;
    }

    button.disabled = false;
    if (status.has_run) {
      const summary =
        `Revisión terminada: ${status.imported} autorizaciones nuevas, ` +
        `${status.updated} actualizadas, ${status.without_text} archivos sin texto, ` +
        `${status.errors} errores de ${status.total} archivos revisados.`;
      const errorSummary = status.error_details.length
        ? ` Primeros errores: ${status.error_details.slice(0, 3).join(" · ")}`
        : "";
      setMessage("user-message", summary + errorSummary, status.errors ? "error" : "success");
      if (status.processed > 0) {
        invalidateLoadedUsers();
        await loadUsers();
        window.dispatchEvent(new Event("records-stale"));
      }
    }
  } catch (error) {
    button.disabled = false;
    setMessage("user-message", error.message, "error");
  }
}

export function loadUsers() {
  const query = document.getElementById("user-query").value.trim();
  if (usersLoadPromise && usersLoadQuery === query) return usersLoadPromise;

  const promise = loadUsersForQuery(query);
  usersLoadPromise = promise;
  usersLoadQuery = query;
  promise.then(
    () => {
      if (usersLoadPromise === promise) {
        usersLoadPromise = null;
        usersLoadQuery = null;
      }
    },
    () => {
      if (usersLoadPromise === promise) {
        usersLoadPromise = null;
        usersLoadQuery = null;
      }
    },
  );
  return promise;
}

async function loadUsersForQuery(query) {
  const generation = ++usersLoadGeneration;
  const list = document.getElementById("user-list");
  list.replaceChildren();
  setMessage("user-message", "Cargando carpetas de usuarios…");
  try {
    const data = await requestJson(`/api/users?q=${encodeURIComponent(query)}`);
    if (generation !== usersLoadGeneration) return false;
    loadedUsers = data.items;
    loadedUsersQuery = query;
    userListWarnings = data.warnings;
    renderUserList();
    if (selectedUser && data.items.some(user => user.name === selectedUser)) {
      return await loadUser(selectedUser);
    }
    return true;
  } catch (error) {
    if (generation !== usersLoadGeneration) return false;
    setMessage("user-message", error.message, "error");
    return false;
  }
}

export function getLoadedUsers() {
  return loadedUsersQuery === "" ? loadedUsers : null;
}

export function invalidateLoadedUsers() {
  usersLoadGeneration += 1;
  loadedUsersQuery = null;
  usersLoadQuery = null;
}

function renderUserList() {
  const list = document.getElementById("user-list");
  const authorizationFilter = document.getElementById("user-authorization-filter").value;
  const statusFilter = document.getElementById("user-status-filter").value;
  const practiceFilter = normalizePractice(document.getElementById("user-practice-filter").value);
  const users = loadedUsers.filter(user => {
    if (authorizationFilter === "with" && !user.has_authorization) return false;
    if (authorizationFilter === "without" && user.has_authorization) return false;
    if (statusFilter && getUserAuthorizationStatus(user).filterValue !== statusFilter) return false;
    if (practiceFilter && !(user.authorization_records || []).some(record =>
      normalizePractice(record.practice).includes(practiceFilter)
    )) return false;
    return true;
  });

  list.replaceChildren();
  for (const user of users) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `user-list-item${user.name === selectedUser ? " active" : ""}`;
    button.dataset.user = user.name;
    button.append(element("span", user.name, "user-list-name"));
    button.append(element(
      "span",
      `${user.document_count} documentos · ${user.imported_count} importados`,
      "user-list-count",
    ));
    const status = getUserAuthorizationStatus(user);
    button.append(element(
      "span",
      status.label,
      `authorization-status user-list-authorization ${status.className}`,
    ));
    list.append(button);
  }
  if (!users.length) {
    list.append(element(
      "p",
      loadedUsers.length
        ? "No hay usuarios que coincidan con los filtros."
        : "No se encontraron carpetas de usuario.",
      "empty-list",
    ));
  }
  const warning = userListWarnings
    ? ` Se omitieron ${userListWarnings} carpetas inaccesibles.`
    : "";
  setMessage("user-message", `${users.length} de ${loadedUsers.length} usuario(s).${warning}`);
}

function normalizePractice(value) {
  return (value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("es")
    .trim()
    .replace(/\s+/g, " ");
}

async function loadUser(name) {
  selectedUser = name;
  document.querySelectorAll(".user-list-item").forEach(button => {
    button.classList.toggle("active", button.dataset.user === name);
  });
  setMessage("user-message", `Abriendo ficha de ${name}…`);
  try {
    const data = await requestJson(`/api/user?name=${encodeURIComponent(name)}`);
    renderUserProfile(data);
    const activeUser = document.querySelector(".user-list-item.active .user-list-count");
    if (activeUser) {
      activeUser.textContent =
        `${data.user.document_count} documentos · ${data.user.imported_count} importados`;
    }
    const warning = data.warnings
      ? ` Se omitieron ${data.warnings} ubicaciones inaccesibles.`
      : "";
    setMessage("user-message", `Ficha de ${name} cargada.${warning}`);
    return true;
  } catch (error) {
    setMessage("user-message", error.message, "error");
    return false;
  }
}

function renderUserProfile(data) {
  const profile = document.getElementById("user-profile");
  profile.className = "user-profile";
  profile.replaceChildren();

  const heading = element("div", undefined, "user-profile-heading");
  heading.append(element("h3", data.user.name));
  heading.append(element(
    "p",
    `${data.user.document_count} documento(s) · ${data.user.imported_count} importado(s)`,
  ));
  profile.append(heading);

  const certificatePaths = new Set(
    data.files.filter(file => file.is_authorization_certificate).map(file => file.path),
  );
  const certificateRecords = data.records.filter(record => certificatePaths.has(record.source_path));
  const otherRecords = data.records.filter(record => !certificatePaths.has(record.source_path));
  profile.append(renderAuthorizationSection(data.files, certificateRecords));

  if (otherRecords.length) {
    profile.append(element("h4", "Otros datos importados"));
    profile.append(renderImportedRecords(otherRecords));
  }

  profile.append(element("h4", `Documentos de la carpeta (${data.files.length})`));
  if (!data.files.length) {
    profile.append(element("p", "Esta carpeta no contiene documentos compatibles.", "empty-list"));
  } else {
    profile.append(renderDocuments(data.files));
  }
  profile.append(renderPreviewPanel("user"));
}

function renderPreviewPanel(scope) {
  const panel = element("aside", undefined, "preview-panel user-preview-panel");
  const heading = element("div", undefined, "preview-title");
  const title = element("h3", "Vista previa");
  title.id = `${scope}-preview-title`;
  heading.append(title);
  const download = element("a", "Descargar", "button secondary hidden");
  download.id = `${scope}-download-link`;
  download.href = "#";
  heading.append(download);
  panel.append(heading);
  panel.append(element(
    "div",
    "Selecciona un documento para verlo aquí.",
    "preview-content empty-state",
  ));
  panel.lastElementChild.id = `${scope}-preview-content`;
  return panel;
}

function renderAuthorizationSection(files, records) {
  const section = element("section", undefined, "authorization-section");
  const orderedRecords = [...records].sort(compareAuthorizationRecency);
  const heading = element("div", undefined, "authorization-section-heading");
  heading.append(element("h4", "Autorización individual"));
  if (orderedRecords.length) {
    const currentStatus = authorizationStatus(orderedRecords[0].expiry_date);
    heading.append(element(
      "span",
      `Estado actual: ${currentStatus.label}`,
      `authorization-status ${currentStatus.className}`,
    ));
  }
  section.append(heading);
  const certificateFiles = files.filter(file => file.is_authorization_certificate);

  if (!certificateFiles.length) {
    section.append(renderAuthorizationNotice(
      "No tiene autorización individual",
      "No se encontró un certificado de autorización individual en esta carpeta.",
      "is-empty",
    ));
    return section;
  }

  if (!orderedRecords.length) {
    section.append(renderAuthorizationNotice(
      "Certificado pendiente de importar",
      `Se encontró ${certificateFiles.length === 1 ? "el certificado" : "certificados"}, pero todavía no hay una autorización registrada. Impórtalo desde la lista de documentos: ${certificateFiles.map(file => file.name).join(" · ")}`,
      "is-pending",
    ));
  } else {
    for (const record of orderedRecords) section.append(renderAuthorizationCard(record));
  }

  const importedPaths = new Set(orderedRecords.map(record => record.source_path));
  const pendingFiles = certificateFiles.filter(file => !importedPaths.has(file.path));
  if (orderedRecords.length && pendingFiles.length) {
    section.append(renderAuthorizationNotice(
      `${pendingFiles.length} certificado(s) pendiente(s)`,
      pendingFiles.map(file => file.name).join(" · "),
      "is-pending",
    ));
  }
  return section;
}

function renderAuthorizationNotice(title, description, variant) {
  const card = element("div", undefined, `authorization-card ${variant}`);
  card.append(element("strong", title));
  card.append(element("p", description));
  return card;
}

function renderAuthorizationCard(record) {
  const status = authorizationStatus(record.expiry_date);
  const card = element("article", undefined, `authorization-card ${status.cardClass}`);
  const heading = element("div", undefined, "authorization-card-heading");
  heading.append(element("h5", "Certificado de autorización individual"));
  heading.append(element(
    "span",
    status.label,
    `authorization-status ${status.className}`,
  ));
  card.append(heading);

  const holder = [record.first_name, record.last_name].filter(Boolean).join(" ");
  const fields = element("div", undefined, "authorization-grid");
  [
    ["Titular", holder],
    ["Cédula", record.identity_number],
    ["N.º de autorización", record.authorization_number],
    ["Práctica", record.practice],
    ["Expedición", record.issue_date],
    ["Vencimiento", record.expiry_date],
  ].forEach(([label, value]) => {
    const field = element("div", undefined, "authorization-field");
    field.append(element("span", label));
    field.append(element("strong", value || "Sin dato"));
    fields.append(field);
  });
  card.append(fields);

  card.append(element("h6", "Equipos asociados"));
  const equipment = (record.equipment || "").split("\n").filter(line => line.trim());
  if (equipment.length) {
    const list = element("ul", undefined, "authorization-equipment");
    equipment.forEach(item => list.append(element("li", item)));
    card.append(list);
  } else {
    card.append(element("p", "No se identificaron equipos asociados.", "empty-list"));
  }
  card.append(element("p", record.source_path, "authorization-source"));
  if (record.needs_review) {
    card.append(element(
      "p",
      "Revisa los campos que aparecen como “Sin dato” antes de usar esta información.",
      "authorization-warning",
    ));
  }
  return card;
}

function parseAuthorizationDate(value) {
  const text = (value || "").trim();
  const dayFirst = text.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  const yearFirst = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  const year = Number(yearFirst ? yearFirst[1] : dayFirst && dayFirst[3]);
  const month = Number(yearFirst ? yearFirst[2] : dayFirst && dayFirst[2]);
  const day = Number(yearFirst ? yearFirst[3] : dayFirst && dayFirst[1]);
  if (!year || !month || !day) return null;
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return null;
  }
  return date;
}

function addCalendarMonths(date, months) {
  const firstOfTargetMonth = new Date(date.getFullYear(), date.getMonth() + months, 1);
  const lastDay = new Date(
    firstOfTargetMonth.getFullYear(),
    firstOfTargetMonth.getMonth() + 1,
    0,
  ).getDate();
  return new Date(
    firstOfTargetMonth.getFullYear(),
    firstOfTargetMonth.getMonth(),
    Math.min(date.getDate(), lastDay),
  );
}

function authorizationStatus(expiryValue) {
  const expiryDate = parseAuthorizationDate(expiryValue);
  if (!expiryDate) {
    return {
      label: "SIN FECHA DE VENCIMIENTO",
      className: "is-unknown",
      cardClass: "is-unknown",
      filterValue: "unknown",
    };
  }

  const today = new Date();
  const todayDate = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  if (expiryDate < todayDate) {
    return {
      label: "VENCIDA",
      className: "is-expired",
      cardClass: "is-expired",
      filterValue: "expired",
    };
  }
  if (expiryDate <= addCalendarMonths(todayDate, 2)) {
    return {
      label: "PRÓXIMO A VENCERSE",
      className: "is-soon",
      cardClass: "is-soon",
      filterValue: "soon",
    };
  }
  return {
    label: "VIGENTE",
    className: "is-valid",
    cardClass: "is-valid",
    filterValue: "valid",
  };
}

export function getUserAuthorizationStatus(user) {
  if (!user.has_authorization) {
    return {
      label: "SIN AUTORIZACIÓN",
      className: "is-empty",
      filterValue: "none",
    };
  }
  const records = user.authorization_records || [];
  if (!records.length) {
    return {
      label: "PENDIENTE DE IMPORTAR",
      className: "is-pending",
      filterValue: "pending",
    };
  }
  const latestRecord = [...records].sort(compareAuthorizationRecency)[0];
  return authorizationStatus(latestRecord.expiry_date);
}

export function compareAuthorizationRecency(left, right) {
  const leftVersion = parseAuthorizationVersion(left.authorization_number);
  const rightVersion = parseAuthorizationVersion(right.authorization_number);
  if (leftVersion && rightVersion && leftVersion.number === rightVersion.number) {
    const renewalOrder = rightVersion.renewals - leftVersion.renewals;
    if (renewalOrder) return renewalOrder;
    const modificationOrder = rightVersion.modifications - leftVersion.modifications;
    if (modificationOrder) return modificationOrder;
  }

  const leftDate = parseAuthorizationDate(left.issue_date) || parseAuthorizationDate(left.expiry_date);
  const rightDate = parseAuthorizationDate(right.issue_date) || parseAuthorizationDate(right.expiry_date);
  if (leftDate && rightDate) {
    const dateOrder = rightDate - leftDate;
    return dateOrder || right.source_path.localeCompare(left.source_path, "es");
  }
  if (leftDate) return -1;
  if (rightDate) return 1;
  return right.source_path.localeCompare(left.source_path, "es");
}

function parseAuthorizationVersion(value) {
  const match = (value || "").match(/^\s*(\d+)\s*\/\s*(\d+)\s*\/\s*(\d+)\s*$/);
  if (!match) return null;
  return {
    number: match[1],
    renewals: Number(match[2]),
    modifications: Number(match[3]),
  };
}

function renderImportedRecords(records) {
  const wrap = element("div", undefined, "table-wrap user-records-wrap");
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const heading = document.createElement("tr");
  [
    "Documento",
    "Nombre",
    "Apellido",
    "Cédula",
    "N.º autorización",
    "Práctica",
    "Expedición",
    "Vencimiento",
    "Equipos asociados",
  ].forEach(title => heading.append(element("th", title)));
  head.append(heading);
  table.append(head);

  const body = document.createElement("tbody");
  for (const record of records) {
    const row = document.createElement("tr");
    row.append(element("td", record.source_path));
    row.append(element("td", record.first_name));
    row.append(element("td", record.last_name));
    row.append(element("td", record.identity_number));
    row.append(element("td", record.authorization_number));
    row.append(element("td", record.practice));
    row.append(element("td", record.issue_date));
    row.append(element("td", record.expiry_date));
    row.append(element("td", record.equipment, "equipment-cell"));
    body.append(row);
  }
  table.append(body);
  wrap.append(table);
  return wrap;
}

function renderDocuments(files) {
  const wrap = element("div", undefined, "table-wrap user-files-wrap");
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const heading = document.createElement("tr");
  ["Documento", "Tipo", "Tamaño", "Importación", "Acciones"].forEach(title => {
    heading.append(element("th", title));
  });
  head.append(heading);
  table.append(head);

  const body = document.createElement("tbody");
  for (const file of files) {
    const row = document.createElement("tr");
    row.append(element("td", file.path, "user-file-path"));
    row.append(element("td", file.extension));
    row.append(element("td", formatSize(file.size)));
    row.append(element(
      "td",
      file.record
        ? (file.is_authorization_certificate ? "Certificado importado" : "Datos importados")
        : file.can_import
          ? (file.is_authorization_certificate ? "Certificado pendiente" : "Pendiente")
          : "No automática",
    ));

    const actions = element("td", undefined, "actions");
    if (file.can_preview) {
      const preview = makeButton("Ver", "preview");
      preview.dataset.path = file.path;
      actions.append(preview);
    }
    const reveal = makeButton("Ver en carpeta", "reveal");
    reveal.dataset.path = file.path;
    actions.append(reveal);
    const download = document.createElement("a");
    download.className = "button secondary small";
    download.href = `/api/download?path=${encodeURIComponent(file.path)}`;
    download.textContent = "Descargar";
    actions.append(download);
    if (file.can_import && (!file.record || file.is_authorization_certificate)) {
      const importButton = makeButton(
        file.is_authorization_certificate
          ? (file.record ? "Actualizar certificado" : "Importar certificado")
          : "Importar datos",
        "import",
      );
      importButton.dataset.path = file.path;
      actions.append(importButton);
    }
    row.append(actions);
    body.append(row);
  }
  table.append(body);
  wrap.append(table);
  return wrap;
}

async function handleProfileAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "preview") {
    previewFile(button.dataset.path, "user");
  } else if (button.dataset.action === "reveal") {
    revealFile(button.dataset.path, "user-message");
  } else if (button.dataset.action === "import") {
    button.disabled = true;
    const successMessage = await extractRecord(button.dataset.path, { navigateToRecords: false });
    if (!successMessage) {
      button.disabled = false;
      return;
    }

    window.dispatchEvent(new Event("records-stale"));
    invalidateLoadedUsers();
    if (await loadUsers()) {
      setMessage("user-message", successMessage, "success");
    } else {
      const errorMessage = document.getElementById("user-message").textContent;
      setMessage(
        "user-message",
        `${successMessage} No se pudo recargar la ficha: ${errorMessage}`,
        "error",
      );
    }
  }
}
