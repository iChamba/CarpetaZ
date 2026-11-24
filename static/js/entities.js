import {
  element,
  formatSize,
  initDirectoryToggle,
  makeButton,
  requestJson,
  resetPreviewActions,
  setMessage,
  setPreviewCloseAction,
  setPdfPreviewActions,
} from "./common.js";
import { authorizationStatus, compareAuthorizationRecency } from "./users.js";

const ENTITY_PAGE_SIZE = 100;
const ENTITY_FILE_PAGE_SIZE = 50;
let selectedEntity = "";
let loadedEntities = [];
let entitiesOffset = 0;
let entitiesTotal = 0;
let entitiesLoadGeneration = 0;
let entityProfileRequest = 0;
let completionHandled = true;

export function initEntities() {
  initDirectoryToggle("entity", "instituciones");
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
  const licenseFilter = document.getElementById("entity-license-filter");
  const licenseStatusFilter = document.getElementById("entity-license-status-filter");
  licenseFilter.addEventListener("change", () => {
    licenseStatusFilter.disabled = licenseFilter.value === "without";
    if (licenseStatusFilter.disabled) licenseStatusFilter.value = "";
    entitiesOffset = 0;
    loadEntities(0);
  });
  licenseStatusFilter.addEventListener("change", () => {
    entitiesOffset = 0;
    loadEntities(0);
  });
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
  const generation = ++entitiesLoadGeneration;
  const query = document.getElementById("entity-query").value.trim();
  setMessage("entity-message", "Cargando instituciones…");
  try {
    const params = new URLSearchParams({
      q: query,
      practice: document.getElementById("entity-practice-filter").value,
      department: document.getElementById("entity-department-filter").value,
      locality: document.getElementById("entity-locality-filter").value,
      license: document.getElementById("entity-license-filter").value,
      status: document.getElementById("entity-license-status-filter").value,
      offset: String(offset),
      limit: String(ENTITY_PAGE_SIZE),
    });
    const data = await requestJson(`/api/entities?${params}`);
    if (generation !== entitiesLoadGeneration) return false;
    loadedEntities = data.items;
    entitiesOffset = data.offset;
    entitiesTotal = data.total;
    setFilterOptions("entity-practice-filter", data.practices, "Todas las prácticas");
    setFilterOptions("entity-department-filter", data.departments, "Todos los departamentos");
    setFilterOptions("entity-locality-filter", data.localities, "Todas las localidades");
    renderEntityList();
    if (selectedEntity && data.items.some(entity => entity.name === selectedEntity)) {
      await loadEntity(selectedEntity);
      if (generation !== entitiesLoadGeneration) return false;
    } else {
      if (selectedEntity) {
        entityProfileRequest += 1;
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
      window.setTimeout(() => {
        if (generation === entitiesLoadGeneration) loadEntities(offset);
      }, 1200);
    } else if (!data.catalog.ready) {
      setMessage("entity-message", data.catalog.message, "error");
    }
    return true;
  } catch (error) {
    if (generation !== entitiesLoadGeneration) return false;
    setMessage("entity-message", error.message, "error");
    return false;
  }
}

async function refreshEntityCatalog() {
  const button = document.getElementById("entity-refresh");
  button.disabled = true;
  setMessage("entity-message", "Iniciando actualización del catálogo…");
  try {
    await requestJson("/api/catalog/refresh", { method: "POST" });
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
    window.dispatchEvent(new Event("records-stale"));
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
  const license = document.getElementById("entity-license-filter").value;
  const status = document.getElementById("entity-license-status-filter").value;
  const entities = loadedEntities.filter(entity =>
    (!practice || entity.practices.includes(practice))
    && (!department || entity.departments.includes(department))
    && (!locality || entity.localities.includes(locality))
    && (!license || entity.has_license === (license === "with"))
    && (!status || entity.license_statuses.includes(status))
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

export async function openLinkedEntity(name) {
  const requestId = ++entityProfileRequest;
  selectedEntity = "";
  document.getElementById("entity-query").value = name;
  for (const id of ["entity-practice-filter", "entity-department-filter", "entity-locality-filter",
    "entity-license-filter", "entity-license-status-filter"]) {
    document.getElementById(id).value = "";
  }
  document.getElementById("entity-license-status-filter").disabled = false;
  const profile = document.getElementById("entity-profile");
  profile.className = "user-profile empty-state";
  profile.textContent = `Abriendo ficha de ${name}…`;
  const loaded = await loadEntities(0);
  if (requestId !== entityProfileRequest) return false;
  if (!loaded) {
    profile.textContent = "No se pudo abrir la ficha de la entidad.";
    return false;
  }
  return loadEntity(name);
}

async function loadEntity(name) {
  const requestId = ++entityProfileRequest;
  selectedEntity = name;
  renderEntityList();
  const profile = document.getElementById("entity-profile");
  profile.className = "user-profile empty-state";
  profile.textContent = `Abriendo ficha de ${name}…`;
  setMessage("entity-message", `Abriendo documentos de ${name}…`);
  try {
    const data = await requestJson(`/api/entity?name=${encodeURIComponent(name)}`);
    if (requestId !== entityProfileRequest) return false;
    renderEntityProfile(data);
    const warning = data.warnings
      ? ` Se omitieron ${data.warnings} ubicaciones inaccesibles.`
      : "";
    setMessage("entity-message", `${data.entity.document_count} documento(s) en ${name}.${warning}`);
    return true;
  } catch (error) {
    if (requestId !== entityProfileRequest) return false;
    profile.textContent = "No se pudo abrir la ficha de la entidad.";
    setMessage("entity-message", error.message, "error");
    return false;
  }
}

function renderLicenseCard(card) {
  const status = authorizationStatus(card.expiry_date);
  const article = element("article", undefined, `authorization-card ${status.cardClass}`);
  const heading = element("div", undefined, "authorization-card-heading");
  const title = element("div", undefined, "authorization-card-title");
  title.append(element("h5", card.license_number));
  const sourceButton = makeButton("Ver documento", "preview", "button secondary small");
  sourceButton.dataset.path = card.source_path;
  title.append(sourceButton);
  heading.append(
    title,
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
  const technicalTables = card.technical_tables || [];
  const pendingTables = card.technical_tables_pending || !card.technical_tables;
  const technicalDetails = element("details", undefined, "authorization-equipment-details");
  technicalDetails.append(element(
    "summary",
    `Equipos y sistemas (${technicalTables.length} tablas${pendingTables ? " · pendiente de procesar" : ""})`,
  ));
  if (pendingTables) {
    technicalDetails.append(element(
      "p",
      "Hay documentos pendientes de revisar para extraer sus tablas.",
      "empty-list",
    ));
  }
  if (!technicalTables.length && !pendingTables) {
    technicalDetails.append(element(
      "p",
      "No se identificaron tablas de equipos o sistemas. Consultá los documentos originales.",
      "empty-list",
    ));
  }
  for (const data of technicalTables) {
    const section = element("section", undefined, "license-technical-table");
    const firstRowValues = (data.rows[0] || []).filter(cell => cell.trim());
    const firstRowTitle = firstRowValues.length === 1 && /^equipos?\s+generadores?\s+de\s*(?:r\s*\.?\s*i\.?|radiaci[oó]n(?:es)?\s+ionizantes?)\s*[:.]?$/i.test(firstRowValues[0])
      ? firstRowValues[0]
      : "";
    section.append(element("h6", `${firstRowTitle || data.title} · ${data.source_name}`));
    if (data.heading && data.heading.trim().toLocaleLowerCase("es") !== firstRowTitle.toLocaleLowerCase("es")) {
      section.append(element("p", data.heading));
    }
    const rows = data.rows.slice(firstRowTitle ? 1 : 0);
    const columnCount = rows.reduce((max, row) => Math.max(max, row.length), 0);
    const visibleColumns = Array.from({ length: columnCount }, (_, index) => index)
      .filter(index => rows.some(row => (row[index] || "").trim()));
    const wrap = element("div", undefined, "table-wrap");
    const table = document.createElement("table");
    const body = document.createElement("tbody");
    for (const cells of rows) {
      const row = document.createElement("tr");
      for (const index of visibleColumns) row.append(element("td", cells[index] || ""));
      body.append(row);
    }
    table.append(body);
    wrap.append(table);
    section.append(wrap);
    technicalDetails.append(section);
  }
  article.append(technicalDetails);
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
  const record = element("section", undefined, "entity-record");
  profile.append(record);
  loadEntityRecord(data.entity.name, record);

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
      if (area.technical_opinions.length) {
        const opinions = element("details", undefined, "entity-technical-opinions");
        opinions.append(element(
          "summary",
          `Dictámenes técnicos (DT) (${area.technical_opinions.length})`,
        ));
        const list = element("ul", undefined, "authorization-equipment");
        for (const opinion of area.technical_opinions) {
          const item = element("li");
          item.append(element("span", opinion.name));
          if (opinion.can_preview) {
            const preview = makeButton("Ver", "preview");
            preview.dataset.path = opinion.path;
            item.append(" ", preview);
          }
          list.append(item);
        }
        opinions.append(list);
        areaSection.append(opinions);
      }
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

function renderEntityFolderGroups(folders, areaPath) {
  const groups = element("div", undefined, "entity-folder-groups");
  for (const folder of folders) {
    const section = element("section", undefined, "entity-folder-group");
    const label = folder.is_root ? "Carpeta principal" : folder.name;
    const toggle = makeButton(
      `${label} (${folder.document_count})`,
      "toggle-entity-folder",
      "entity-folder-toggle",
    );
    toggle.dataset.path = areaPath;
    toggle.dataset.folder = folder.name;
    toggle.dataset.root = String(folder.is_root);
    toggle.setAttribute("aria-expanded", "false");
    const content = element("div", undefined, "entity-folder-content hidden");
    section.append(toggle, content);
    groups.append(section);
  }
  if (!folders.length) {
    groups.append(element("p", "No se encontraron documentos en esta institución.", "empty-list"));
  }
  return groups;
}

function renderEntityDocuments(files, areaPath, folder, isRoot, offset, limit, total) {
  const wrap = element("div", undefined, "table-wrap user-files-wrap entity-documents-wrap");
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  ["Documento", "Tipo", "Tamaño", "Creado", "Acciones"].forEach(label => {
    header.append(element("th", label));
  });
  head.append(header);
  table.append(head);

  const body = document.createElement("tbody");
  for (const file of files) {
    const row = document.createElement("tr");
    const areaPrefix = `${areaPath}/`;
    const displayPath = file.path.startsWith(areaPrefix)
      ? file.path.slice(areaPrefix.length)
      : file.path;
    const pathCell = element("td", undefined, "user-file-path");
    const filenameIndex = Math.max(displayPath.lastIndexOf("/"), displayPath.lastIndexOf("\\")) + 1;
    pathCell.append(document.createTextNode(displayPath.slice(0, filenameIndex)));
    pathCell.append(element("strong", displayPath.slice(filenameIndex)));
    pathCell.title = file.path;
    row.append(pathCell);
    row.append(element("td", file.extension));
    row.append(element("td", formatSize(file.size)));
    row.append(element("td", (file.created || "").slice(0, 10)));
    const actions = element("td", undefined, "actions");
    if (file.can_preview) {
      const preview = makeButton("Ver", "preview");
      preview.dataset.path = file.path;
      actions.append(preview);
    }
    const reveal = makeButton("Carpeta", "reveal");
    reveal.dataset.path = file.path;
    actions.append(reveal);
    if (file.can_import) {
      const extract = makeButton(
        file.license_number ? "Actualizar licencia" : "Extraer licencia",
        "extract",
      );
      extract.dataset.path = file.path;
      actions.append(extract);
    }
    row.append(actions);
    body.append(row);
  }
  if (!total) {
    const row = document.createElement("tr");
    row.append(element("td", "No se encontraron documentos en esta carpeta.", undefined));
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
  previous.dataset.folder = folder;
  previous.dataset.root = String(isRoot);
  previous.dataset.offset = String(Math.max(0, offset - limit));
  previous.disabled = offset <= 0;
  const next = makeButton("Siguiente", "entity-files-page", "button secondary small");
  next.dataset.path = areaPath;
  next.dataset.folder = folder;
  next.dataset.root = String(isRoot);
  next.dataset.offset = String(offset + limit);
  next.disabled = offset + files.length >= total;
  controls.append(previous, next);
  pagination.append(controls);
  wrap.append(pagination);
  return wrap;
}

async function loadEntityAreaFiles(areaPath, container) {
  const params = new URLSearchParams({
    name: selectedEntity,
    area: areaPath,
    folders_only: "1",
  });
  container.textContent = "Cargando subcarpetas…";
  try {
    const data = await requestJson(`/api/entity?${params}`);
    container.replaceChildren(renderEntityFolderGroups(data.folders, areaPath));
    return true;
  } catch (error) {
    container.textContent = "";
    setMessage("entity-message", error.message, "error");
    return false;
  }
}

async function loadEntityFolderFiles(areaPath, folder, isRoot, offset, container) {
  const params = new URLSearchParams({
    name: selectedEntity,
    area: areaPath,
    offset: String(offset),
    limit: String(ENTITY_FILE_PAGE_SIZE),
  });
  if (isRoot) {
    params.set("root", "1");
  } else {
    params.set("folder", folder);
  }
  container.textContent = "Cargando documentos…";
  try {
    const data = await requestJson(`/api/entity?${params}`);
    container.replaceChildren(renderEntityDocuments(
      data.files,
      areaPath,
      folder,
      isRoot,
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
  resetPreviewActions(title);
  title.textContent = path;
  content.className = "preview-content";
  content.replaceChildren();
  const extension = path.split(".").pop().toLowerCase();
  const url = `/api/entity/preview?path=${encodeURIComponent(path)}`;
  if (extension === "pdf") {
    const frame = document.createElement("iframe");
    frame.title = `Vista previa de ${path}`;
    frame.src = url;
    content.append(frame);
    setPdfPreviewActions(title, frame, closeEntityPreview);
  } else {
    setPreviewCloseAction(title, closeEntityPreview);
    if (["jpg", "jpeg", "png"].includes(extension)) {
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
}

function closeEntityPreview() {
  const content = document.getElementById("entity-preview-content");
  const title = document.querySelector("#entity-profile .preview-title h3");
  content.className = "preview-content empty-state";
  content.textContent = "Selecciona un documento para previsualizarlo aquí.";
  title.textContent = "Vista previa";
  resetPreviewActions(title);
  const browser = document.querySelector("#entity-profile .preview-browser-layout");
  if (browser) browser.classList.add("preview-hidden");
}

async function handleEntityAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "load-area-files") {
    button.disabled = true;
    const container = button.nextElementSibling;
    if (await loadEntityAreaFiles(button.dataset.path, container)) {
      button.remove();
    } else {
      button.disabled = false;
    }
  } else if (button.dataset.action === "toggle-entity-folder") {
    const content = button.nextElementSibling;
    const expanded = button.getAttribute("aria-expanded") === "true";
    if (expanded) {
      button.setAttribute("aria-expanded", "false");
      content.classList.add("hidden");
      return;
    }

    button.setAttribute("aria-expanded", "true");
    content.classList.remove("hidden");
    if (content.dataset.loaded === "true") return;

    button.disabled = true;
    const loaded = await loadEntityFolderFiles(
      button.dataset.path,
      button.dataset.folder,
      button.dataset.root === "true",
      0,
      content,
    );
    button.disabled = false;
    if (loaded) {
      content.dataset.loaded = "true";
    } else {
      button.setAttribute("aria-expanded", "false");
      content.classList.add("hidden");
    }
  } else if (button.dataset.action === "entity-files-page") {
    button.disabled = true;
    await loadEntityFolderFiles(
      button.dataset.path,
      button.dataset.folder,
      button.dataset.root === "true",
      Number(button.dataset.offset),
      button.closest(".entity-folder-content"),
    );
    button.disabled = false;
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
      window.dispatchEvent(new Event("records-stale"));
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
      window.dispatchEvent(new Event("records-stale"));
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

// Ficha de la entidad (datos que se van juntando) y equipos con vencimientos.
const recordFields = [
  ["rut", "RUT", "text", 100], ["phone", "Teléfono", "tel", 100],
  ["email", "Email", "email", 254], ["contact_details", "Datos de contacto", "text", 255],
  ["address", "Domicilio", "text", 255], ["city", "Ciudad", "text", 160],
  ["department", "Departamento", "text", 160],
];
const equipmentStatus = {
  vencida: ["Vencida", "workflow-deadline-overdue"],
  proxima: ["Próxima (90 días)", "workflow-deadline-warning"],
  al_dia: ["Al día", "workflow-deadline-on-time"],
  sin_fecha: ["Sin próxima fecha", "planning-status-none"],
};
let entityRecordRequest = 0;

function recordDate(value) {
  if (!value) return "—";
  const [year, month, day] = value.split("-");
  return `${day}/${month}/${year}`;
}

async function loadEntityRecord(name, container) {
  const requestId = ++entityRecordRequest;
  container.replaceChildren(element("p", "Cargando ficha y equipos…", "entity-file-status"));
  try {
    const data = await requestJson(`/api/entity/record?name=${encodeURIComponent(name)}`);
    if (requestId === entityRecordRequest) renderEntityRecord(data, container);
  } catch (error) {
    if (requestId === entityRecordRequest) {
      container.replaceChildren(element("p", `No se pudo cargar la ficha: ${error.message}`, "entity-file-status"));
    }
  }
}

async function postEntityRecord(url, payload, container, success) {
  try {
    const data = await requestJson(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    renderEntityRecord(data, container);
    setMessage("entity-message", success, "success");
  } catch (error) {
    setMessage("entity-message", error.message, "error");
    container.querySelectorAll("button").forEach(button => { button.disabled = false; });
  }
}

function equipmentTable(items, data, container, mode) {
  const table = element("table", undefined, "workflow-table entity-equipment-table");
  const head = element("tr");
  const headings = ["Institución en planilla", "Práctica / equipo", "Departamento", "Última inspección",
    "Próxima inspección", "Estado", "Equipos", "Planilla"];
  if (mode === "suggested" && data.can_link) headings.unshift("");
  if (mode === "linked" && data.can_link) headings.push("");
  for (const heading of headings) head.append(element("th", heading));
  const thead = element("thead");
  thead.append(head);
  const body = element("tbody");
  for (const item of items) {
    const row = element("tr");
    if (mode === "suggested" && data.can_link) {
      const cell = element("td");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.value = String(item.id);
      box.setAttribute("aria-label", `Asociar ${item.institution} · ${item.practice}`);
      cell.append(box);
      row.append(cell);
    }
    const [label, className] = equipmentStatus[item.status] || [item.status, "planning-status-none"];
    const status = element("td");
    status.append(element("span", label, `workflow-deadline-badge ${className}`));
    row.append(element("td", item.institution), element("td", item.practice || "—"),
      element("td", item.department || "—"), element("td", recordDate(item.last_inspection)),
      element("td", recordDate(item.next_inspection)), status,
      element("td", String(item.equipment_count)), element("td", item.categories.join(", ")));
    if (mode === "linked" && data.can_link) {
      const cell = element("td");
      const remove = element("button", "Quitar", "button secondary small");
      remove.type = "button";
      remove.addEventListener("click", () => {
        remove.disabled = true;
        postEntityRecord("/api/entity/equipment",
          { name: data.entity_name, item_ids: [item.id], action: "unlink" }, container, "Equipo desasociado.");
      });
      cell.append(remove);
      row.append(cell);
    }
    body.append(row);
  }
  table.append(thead, body);
  const wrap = element("div", undefined, "table-wrap");
  wrap.append(table);
  return wrap;
}

function renderEntityRecord(data, container) {
  container.replaceChildren();
  // Datos de la entidad.
  const form = element("form", undefined, "workflow-form entity-record-form");
  form.append(element("h4", "Datos de la entidad"));
  for (const [field, label, type, max] of recordFields) {
    const input = document.createElement("input");
    input.name = field;
    input.type = type;
    input.maxLength = max;
    input.value = data.profile[field] || "";
    input.readOnly = !data.can_edit;
    const wrapper = element("label", label);
    wrapper.append(input);
    form.append(wrapper);
  }
  const notes = document.createElement("textarea");
  notes.name = "notes";
  notes.maxLength = 2000;
  notes.rows = 2;
  notes.value = data.profile.notes || "";
  notes.readOnly = !data.can_edit;
  const notesLabel = element("label", "Notas", "entity-record-wide");
  notesLabel.append(notes);
  form.append(notesLabel);
  form.append(element("p", data.can_edit
    ? "Se completa sola con los datos de las inspecciones asociadas (solo los campos vacíos); lo que cargues acá no se reemplaza."
    : "Solo Secretaría, Jefe de Inspección o un administrador pueden editar la ficha.", "form-help entity-record-wide"));
  if (data.can_edit) {
    const save = element("button", "Guardar ficha", "button workflow-approve");
    save.type = "submit";
    const actions = element("div", undefined, "entity-record-wide");
    actions.append(save);
    form.append(actions);
    form.addEventListener("submit", event => {
      event.preventDefault();
      save.disabled = true;
      postEntityRecord("/api/entity/record", {
        name: data.entity_name, version: data.profile.version,
        ...Object.fromEntries(new FormData(form)),
      }, container, "Ficha guardada.");
    });
  }
  if (data.events.length) {
    const history = element("details", undefined, "entity-record-wide entity-record-history");
    history.append(element("summary", `Historial de la ficha (${data.events.length})`));
    const list = element("ol");
    for (const event of data.events) {
      const item = element("li");
      item.append(element("strong", event.actor_name),
        ` · ${event.action} · ${new Date(event.created_at).toLocaleString("es-UY")}` +
        (event.details_text ? ` · ${event.details_text}` : ""));
      list.append(item);
    }
    history.append(list);
    form.append(history);
  }
  container.append(form);

  // Equipos y vencimientos.
  const equipment = element("section", undefined, "entity-record-equipment");
  equipment.append(element("h4", `Equipos y vencimientos (${data.equipment.length})`));
  if (data.equipment.length) {
    equipment.append(equipmentTable(data.equipment, data, container, "linked"));
  } else {
    equipment.append(element("p", "Todavía no hay equipos de la planificación asociados a esta entidad.", "entity-file-status"));
  }
  if (data.suggested_equipment.length) {
    const suggested = element("details", undefined, "entity-record-suggested");
    suggested.open = !data.equipment.length;
    suggested.append(element("summary",
      `Posibles equipos de esta entidad según el nombre en las planillas (${data.suggested_equipment.length})`));
    suggested.append(equipmentTable(data.suggested_equipment, data, container, "suggested"));
    if (data.can_link) {
      const link = element("button", "Asociar seleccionados", "button workflow-approve small");
      link.type = "button";
      link.addEventListener("click", () => {
        const ids = [...suggested.querySelectorAll('input[type="checkbox"]:checked')].map(box => Number(box.value));
        if (!ids.length) {
          setMessage("entity-message", "Marca al menos un equipo para asociar.", "error");
          return;
        }
        link.disabled = true;
        postEntityRecord("/api/entity/equipment", { name: data.entity_name, item_ids: ids, action: "link" },
          container, `${ids.length} equipo(s) asociados.`);
      });
      suggested.append(link);
    } else {
      suggested.append(element("p", "Solo Jefe de Inspección o un administrador pueden asociar equipos.", "form-help"));
    }
    equipment.append(suggested);
  }
  container.append(equipment);
}
