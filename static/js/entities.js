import { appendYearGroups, element, formatSize, makeButton, requestJson, setMessage } from "./common.js";
import { authorizationStatus, compareAuthorizationRecency } from "./users.js";

const ENTITY_PAGE_SIZE = 100;
const ENTITY_FILE_PAGE_SIZE = 50;
let selectedEntity = "";
let loadedEntities = [];
let entitiesOffset = 0;
let entitiesTotal = 0;
let completionHandled = true;

export function initEntities() {
  document.getElementById("entity-filter").addEventListener("submit", event => {
    event.preventDefault();
    entitiesOffset = 0;
    loadEntities(0);
  });
  document.getElementById("entity-refresh").addEventListener("click", refreshEntityCatalog);
  for (const id of [
    "entity-practice-filter",
    "entity-department-filter",
    "entity-locality-filter",
  ]) {
    document.getElementById(id).addEventListener("change", () => {
      entitiesOffset = 0;
      loadEntities(0);
    });
  }
  document.getElementById("entity-prev").addEventListener("click", () => {
    entitiesOffset = Math.max(0, entitiesOffset - ENTITY_PAGE_SIZE);
    loadEntities(entitiesOffset);
  });
  document.getElementById("entity-next").addEventListener("click", () => {
    entitiesOffset += ENTITY_PAGE_SIZE;
    loadEntities(entitiesOffset);
  });
  document.getElementById("entity-list").addEventListener("click", event => {
    const button = event.target.closest("button[data-entity]");
    if (button) loadEntity(button.dataset.entity);
  });
  document.getElementById("entity-profile").addEventListener("click", handleEntityAction);
  document.getElementById("import-entity-licenses").addEventListener("click", startEntityImport);
  refreshEntityImportStatus();
}

export async function loadEntities(offset = entitiesOffset) {
  const query = document.getElementById("entity-query").value.trim();
  setMessage("entity-message", "Cargando instituciones…");
  try {
    const params = new URLSearchParams({
      q: query,
      practice: document.getElementById("entity-practice-filter").value,
      department: document.getElementById("entity-department-filter").value,
      locality: document.getElementById("entity-locality-filter").value,
      offset: String(offset),
      limit: String(ENTITY_PAGE_SIZE),
    });
    const data = await requestJson(`/api/entities?${params}`);
    loadedEntities = data.items;
    entitiesOffset = data.offset;
    entitiesTotal = data.total;
    setFilterOptions("entity-practice-filter", data.practices, "Todas las prácticas");
    setFilterOptions("entity-department-filter", data.departments, "Todos los departamentos");
    setFilterOptions("entity-locality-filter", data.localities, "Todas las localidades");
    renderEntityList();
    if (selectedEntity && data.items.some(entity => entity.name === selectedEntity)) {
      await loadEntity(selectedEntity);
    } else {
      if (selectedEntity) {
        selectedEntity = "";
        const profile = document.getElementById("entity-profile");
        profile.className = "user-profile empty-state";
        profile.textContent = "Selecciona una institución para revisar sus áreas y documentos.";
      }
      setMessage(
        "entity-message",
        `${entitiesTotal} institución(es).${data.warnings ? ` Se omitieron ${data.warnings} ubicaciones inaccesibles.` : ""}`,
      );
    }
    if (!data.catalog.ready && data.catalog.running) {
      setMessage(
        "entity-message",
        `${data.catalog.message}: ${data.catalog.processed} archivo(s) catalogados`,
      );
      window.setTimeout(() => loadEntities(offset), 1200);
    } else if (!data.catalog.ready) {
      setMessage("entity-message", data.catalog.message, "error");
    }
    return true;
  } catch (error) {
    setMessage("entity-message", error.message, "error");
    return false;
  }
}

async function refreshEntityCatalog() {
  const button = document.getElementById("entity-refresh");
  button.disabled = true;
  setMessage("entity-message", "Iniciando actualización del catálogo…");
  try {
    await requestJson("/api/index", { method: "POST" });
    await waitForEntityCatalog();
  } catch (error) {
    button.disabled = false;
    setMessage("entity-message", error.message, "error");
  }
}

async function waitForEntityCatalog() {
  const button = document.getElementById("entity-refresh");
  try {
    const status = await requestJson("/api/status");
    if (status.catalog_running) {
      setMessage(
        "entity-message",
        `${status.catalog_message}: ${status.catalog_processed} archivo(s)`,
      );
      window.setTimeout(waitForEntityCatalog, 1200);
      return;
    }
    button.disabled = false;
    await loadEntities(0);
  } catch (error) {
    button.disabled = false;
    setMessage("entity-message", error.message, "error");
  }
}

function setFilterOptions(id, values, placeholder) {
  const select = document.getElementById(id);
  const currentValue = select.value;
  select.replaceChildren(new Option(placeholder, ""));
  for (const value of values) {
    select.add(new Option(value, value));
  }
  select.value = values.includes(currentValue) ? currentValue : "";
}

function renderEntityList() {
  const list = document.getElementById("entity-list");
  const practice = document.getElementById("entity-practice-filter").value;
  const department = document.getElementById("entity-department-filter").value;
  const locality = document.getElementById("entity-locality-filter").value;
  const entities = loadedEntities.filter(entity =>
    (!practice || entity.practices.includes(practice))
    && (!department || entity.departments.includes(department))
    && (!locality || entity.localities.includes(locality))
  );
  list.replaceChildren();
  for (const entity of entities) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `user-list-item${entity.name === selectedEntity ? " active" : ""}`;
    button.dataset.entity = entity.name;
    button.append(element("span", entity.name, "user-list-name"));
    button.append(element(
      "span",
      `${entity.area_count} áreas · ${entity.document_count} documentos · ${entity.imported_count} licencias encontradas`,
      "user-list-count",
    ));
    button.append(element("span", entity.practices.join(" · "), "user-list-count"));
    list.append(button);
  }
  if (!entities.length) {
    list.append(element(
      "p",
      loadedEntities.length
        ? "Ninguna institución coincide con los filtros."
        : "No se encontraron carpetas de instituciones.",
      "empty-list",
    ));
  }
  const first = entitiesTotal ? entitiesOffset + 1 : 0;
  const last = Math.min(entitiesOffset + loadedEntities.length, entitiesTotal);
  document.getElementById("entity-page-label").textContent =
    `${first}–${last} de ${entitiesTotal}`;
  document.getElementById("entity-prev").disabled = entitiesOffset <= 0;
  document.getElementById("entity-next").disabled =
    entitiesOffset + loadedEntities.length >= entitiesTotal;
}

async function loadEntity(name) {
  selectedEntity = name;
  renderEntityList();
  setMessage("entity-message", `Abriendo documentos de ${name}…`);
  try {
    const data = await requestJson(`/api/entity?name=${encodeURIComponent(name)}`);
    renderEntityProfile(data);
    const warning = data.warnings
      ? ` Se omitieron ${data.warnings} ubicaciones inaccesibles.`
      : "";
    setMessage("entity-message", `${data.entity.document_count} documento(s) en ${name}.${warning}`);
    return true;
  } catch (error) {
    setMessage("entity-message", error.message, "error");
    return false;
  }
}

function renderLicenseCard(card) {
  const status = authorizationStatus(card.expiry_date);
  const article = element("article", undefined, `authorization-card ${status.cardClass}`);
  const heading = element("div", undefined, "authorization-card-heading");
  heading.append(
    element("h5", card.license_number),
    element("span", status.label, `authorization-status ${status.className}`),
  );
  article.append(heading);
  const fields = element("div", undefined, "authorization-grid");
  [
    ["Representante legal", card.representative],
    ["N.º de autorización", card.authorization_number],
    ["Expedición", card.issue_date],
    ["Vencimiento", card.expiry_date],
  ].forEach(([label, value]) => {
    const box = element("div", undefined, "authorization-field");
    box.append(element("span", label), element("strong", value || "Sin dato"));
    fields.append(box);
  });
  article.append(fields);
  const list = element("ul", undefined, "authorization-equipment");
  for (const source of card.documents) {
    const item = element("li");
    item.append(element("span", source.name));
    const preview = makeButton("Ver", "preview");
    preview.dataset.path = source.path;
    item.append(" ", preview);
    list.append(item);
  }
  article.append(element("h6", "Documentos de origen"), list);
  return article;
}

function renderLicenseCards(cards) {
  const ordered = [...cards].sort(compareAuthorizationRecency);
  const grid = element("div", undefined, "entity-license-cards");
  grid.append(renderLicenseCard(ordered[0]));
  if (ordered.length > 1) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "button secondary authorization-previous-toggle";
    toggle.dataset.action = "toggle-previous-licenses";
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = `Ver licencias anteriores (${ordered.length - 1})`;
    const previous = element("div", undefined, "authorization-previous hidden");
    ordered.slice(1).forEach(card => previous.append(renderLicenseCard(card)));
    grid.append(toggle, previous);
  }
  return grid;
}

function renderEntityProfile(data) {
  const profile = document.getElementById("entity-profile");
  profile.className = "user-profile";
  profile.replaceChildren();

  const heading = element("div", undefined, "user-profile-heading");
  const title = document.createElement("div");
  const locations = [...new Set(
    data.practices.flatMap(practice => practice.areas.map(formatAreaLocation)),
  )];
  const name = element("h3", undefined, "entity-name-line");
  name.append(element("span", data.entity.name));
  if (locations.length) {
    name.append(element("span", locations.join(" / "), "entity-profile-location"));
  }
  title.append(name);
  title.append(element(
    "p",
    `${data.entity.practice_count} prácticas · ${data.entity.area_count} áreas · ` +
      `${data.entity.document_count} documentos`,
  ));
  heading.append(title);
  profile.append(heading);

  const browser = element(
    "div",
    undefined,
    "preview-browser-layout preview-hidden entity-preview-browser-layout",
  );
  const fileList = element("section", undefined, "preview-file-list entity-files-browser");
  for (const practice of data.practices) {
    const section = element("section", undefined, "entity-practice-group");
    const practiceHeading = element("div", undefined, "entity-practice-heading");
    practiceHeading.append(element("h4", `Práctica: ${practice.name}`));
    section.append(practiceHeading);
    if (practice.license_cards.length) {
      section.append(renderLicenseCards(practice.license_cards));
    } else {
      section.append(element("p", "No se encontraron licencias para esta práctica.", "entity-file-status"));
    }

    for (const area of practice.areas) {
      const areaSection = element("section", undefined, "entity-area");
      const areaHeading = element("div", undefined, "entity-area-heading");
      if (practice.areas.length > 1) {
        areaHeading.append(element("h5", formatAreaLocation(area), "entity-area-location"));
      }
      const openFolder = makeButton("Abrir carpeta", "open-folder");
      openFolder.dataset.path = area.path;
      areaHeading.append(openFolder);
      areaSection.append(areaHeading);
      areaSection.append(element(
        "p",
        `${area.document_count} documentos · ${area.imported_count} con licencia reconocida`,
        "entity-file-status",
      ));
      const loadFiles = makeButton(
        `Cargar documentos (${area.document_count})`,
        "load-area-files",
        "button secondary small",
      );
      loadFiles.dataset.path = area.path;
      const filesContainer = element("div", undefined, "entity-area-files");
      areaSection.append(loadFiles, filesContainer);
      section.append(areaSection);
    }
    fileList.append(section);
  }
  browser.append(fileList, renderEntityPreviewPanel());
  profile.append(browser);
}

function formatAreaLocation(area) {
  const locations = [];
  if (area.department) locations.push(`Dpto. ${area.department}`);
  if (area.locality) locations.push(`Loc. ${area.locality}`);
  return locations.join(" · ");
}

function renderEntityDocuments(files, areaPath, offset, limit, total) {
  const wrap = element("div", undefined, "table-wrap user-files-wrap");
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  ["Documento", "Tipo", "Tamaño", "Creado", "Licencia", "Acciones"].forEach(label => {
    header.append(element("th", label));
  });
  head.append(header);
  table.append(head);

  const body = document.createElement("tbody");
  appendYearGroups(body, files, 6, file => {
    const row = document.createElement("tr");
    const areaPrefix = `${areaPath}/`;
    const displayPath = file.path.startsWith(areaPrefix)
      ? file.path.slice(areaPrefix.length)
      : file.path;
    const pathCell = element("td", displayPath, "user-file-path");
    pathCell.title = file.path;
    row.append(pathCell);
    row.append(element("td", file.extension));
    row.append(element("td", formatSize(file.size)));
    row.append(element("td", (file.created || "").slice(0, 10)));
    row.append(element(
      "td",
      file.license_number || (file.can_import ? "Pendiente" : "No automática"),
    ));
    const actions = element("td", undefined, "actions");
    if (file.can_preview) {
      const preview = makeButton("Ver", "preview");
      preview.dataset.path = file.path;
      actions.append(preview);
    }
    const reveal = makeButton("Carpeta", "reveal");
    reveal.dataset.path = file.path;
    actions.append(reveal);
    const download = document.createElement("a");
    download.className = "button secondary small";
    download.href = `/api/entity/download?path=${encodeURIComponent(file.path)}`;
    download.textContent = "Descargar";
    actions.append(download);
    if (file.can_import) {
      const extract = makeButton(
        file.license_number ? "Actualizar licencia" : "Extraer licencia",
        "extract",
      );
      extract.dataset.path = file.path;
      actions.append(extract);
    }
    row.append(actions);
    return row;
  });
  if (!total) {
    const row = document.createElement("tr");
    row.append(element("td", "No se encontraron documentos en esta institución.", undefined));
    body.append(row);
  }
  table.append(body);
  wrap.append(table);
  const pagination = element("div", undefined, "pagination");
  const first = total ? offset + 1 : 0;
  const last = Math.min(offset + files.length, total);
  pagination.append(element("span", `${first}–${last} de ${total} documentos`));
  const controls = document.createElement("div");
  const previous = makeButton("Anterior", "entity-files-page", "button secondary small");
  previous.dataset.path = areaPath;
  previous.dataset.offset = String(Math.max(0, offset - limit));
  previous.disabled = offset <= 0;
  const next = makeButton("Siguiente", "entity-files-page", "button secondary small");
  next.dataset.path = areaPath;
  next.dataset.offset = String(offset + limit);
  next.disabled = offset + files.length >= total;
  controls.append(previous, next);
  pagination.append(controls);
  wrap.append(pagination);
  return wrap;
}

async function loadEntityAreaFiles(areaPath, offset, container) {
  const params = new URLSearchParams({
    name: selectedEntity,
    area: areaPath,
    offset: String(offset),
    limit: String(ENTITY_FILE_PAGE_SIZE),
  });
  container.textContent = "Cargando documentos…";
  try {
    const data = await requestJson(`/api/entity?${params}`);
    container.replaceChildren(renderEntityDocuments(
      data.files,
      areaPath,
      data.offset,
      data.limit,
      data.total,
    ));
    return true;
  } catch (error) {
    container.textContent = "";
    setMessage("entity-message", error.message, "error");
    return false;
  }
}

function renderEntityPreviewPanel() {
  const panel = element("aside", undefined, "preview-panel user-preview-panel");
  const heading = element("div", undefined, "preview-title");
  heading.append(element("h3", "Vista previa", undefined));
  const download = element("a", "Descargar", "button secondary hidden");
  download.id = "entity-preview-download";
  download.href = "#";
  heading.append(download);
  panel.append(heading);
  panel.append(element(
    "div",
    "Selecciona un documento para previsualizarlo aquí.",
    "preview-content empty-state",
  ));
  panel.lastElementChild.id = "entity-preview-content";
  return panel;
}

async function previewEntityFile(path) {
  const content = document.getElementById("entity-preview-content");
  const title = document.querySelector("#entity-profile .preview-title h3");
  const download = document.getElementById("entity-preview-download");
  title.textContent = path;
  download.href = `/api/entity/download?path=${encodeURIComponent(path)}`;
  download.classList.remove("hidden");
  content.className = "preview-content";
  content.replaceChildren();
  const extension = path.split(".").pop().toLowerCase();
  const url = `/api/entity/preview?path=${encodeURIComponent(path)}`;
  if (extension === "pdf") {
    const frame = document.createElement("iframe");
    frame.title = `Vista previa de ${path}`;
    frame.src = url;
    content.append(frame);
  } else if (["jpg", "jpeg", "png"].includes(extension)) {
    const image = document.createElement("img");
    image.alt = path;
    image.src = url;
    content.append(image);
  } else if (["txt", "doc", "docx"].includes(extension)) {
    try {
      const response = await fetch(url);
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || `Error HTTP ${response.status}`);
      }
      content.append(element("pre", await response.text()));
    } catch (error) {
      setMessage("entity-message", error.message, "error");
    }
  }
}

async function handleEntityAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "load-area-files") {
    button.disabled = true;
    const container = button.nextElementSibling;
    if (await loadEntityAreaFiles(button.dataset.path, 0, container)) {
      button.remove();
    } else {
      button.disabled = false;
    }
  } else if (button.dataset.action === "entity-files-page") {
    await loadEntityAreaFiles(
      button.dataset.path,
      Number(button.dataset.offset),
      button.closest(".entity-area-files"),
    );
  } else if (button.dataset.action === "toggle-previous-licenses") {
    const previous = button.nextElementSibling;
    const expanded = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", String(!expanded));
    previous.classList.toggle("hidden", expanded);
    button.textContent = expanded
      ? `Ver licencias anteriores (${previous.children.length})`
      : "Ocultar licencias anteriores";
  } else if (button.dataset.action === "preview") {
    button.closest(".preview-browser-layout").classList.remove("preview-hidden");
    previewEntityFile(button.dataset.path);
  } else if (button.dataset.action === "reveal") {
    await postEntityAction("/api/entity/open-location", { path: button.dataset.path });
  } else if (button.dataset.action === "open-folder") {
    await postEntityAction("/api/entity/open-folder", { path: button.dataset.path });
  } else if (button.dataset.action === "extract") {
    button.disabled = true;
    try {
      const result = await requestJson("/api/entity/extract", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: button.dataset.path }),
      });
      await loadEntities();
      setMessage(
        "entity-message",
        `Licencia ${result.license_number} ${result.updated ? "actualizada" : "extraída"}.`,
        "success",
      );
    } catch (error) {
      button.disabled = false;
      setMessage("entity-message", error.message, "error");
    }
  }
}

async function postEntityAction(url, data) {
  try {
    await requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    setMessage("entity-message", "Se abrió el Explorador de archivos en la PC que ejecuta la aplicación.", "success");
  } catch (error) {
    setMessage("entity-message", error.message, "error");
  }
}

async function startEntityImport() {
  const button = document.getElementById("import-entity-licenses");
  button.disabled = true;
  completionHandled = false;
  setMessage("entity-message", "Iniciando revisión de documentos…");
  try {
    await requestJson("/api/entity/import", { method: "POST" });
    await refreshEntityImportStatus();
  } catch (error) {
    button.disabled = false;
    completionHandled = true;
    setMessage("entity-message", error.message, "error");
  }
}

async function refreshEntityImportStatus() {
  const button = document.getElementById("import-entity-licenses");
  try {
    const status = await requestJson("/api/entity/import/status");
    if (status.running) {
      button.disabled = true;
      setMessage(
        "entity-message",
        `${status.message}: ${status.imported} nuevas, ${status.updated} actualizadas, ` +
          `${status.without_license} sin licencia, ${status.without_text} sin texto, ${status.errors} errores.`,
      );
      window.setTimeout(refreshEntityImportStatus, 1500);
      return;
    }
    button.disabled = false;
    if (status.has_run && !completionHandled) {
      completionHandled = true;
      const errors = status.error_details.length
        ? ` Primeros errores: ${status.error_details.slice(0, 3).join(" · ")}`
        : "";
      setMessage(
        "entity-message",
        `Revisión terminada: ${status.imported} nuevas, ${status.updated} actualizadas, ` +
          `${status.without_license} sin licencia, ${status.without_text} sin texto y ` +
          `${status.errors} errores de ${status.total} archivos.${errors}`,
        status.errors ? "error" : "success",
      );
      await loadEntities();
    }
  } catch (error) {
    button.disabled = false;
    setMessage("entity-message", error.message, "error");
  }
}
