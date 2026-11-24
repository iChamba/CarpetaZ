import { element, requestJson, setMessage } from "./common.js";

const byId = id => document.getElementById(id);
const labels = {
  application_date: "Fecha de solicitud", case_number: "Número de trámite",
  company_name: "Institución o empresa", rut: "RUT", request_type: "Tipo de solicitud",
  inspection_type: "Tipo de inspección", practice: "Práctica",
  equipment_model: "Modelo de equipo a inspeccionar", contact_details: "Datos de contacto",
  phone: "Teléfono", address: "Domicilio", city: "Ciudad", email: "Email",
  entity_name: "Entidad usuaria",
  inspector_name: "Inspector líder", second_inspector_name: "Segundo inspector",
  scheduled_date: "Fecha de inspección",
  scheduled_time: "Hora de inspección", scheduled_place: "Lugar de inspección",
};
const stages = {
  assignment: "Asignación de inspector",
  pre_inspection: "Checklist previo y agenda",
  post_inspection: "Checklist posterior",
  chief_review: "Revisión de Jefe de Inspección",
  send: "Envío por inspector",
  completed: "Enviada",
};
const eventLabels = {
  created: "Creación", assign: "Inspector asignado",
  complete_pre: "Checklist previo completado e inspección agendada",
  complete_post: "Checklist posterior enviado al jefe",
  return_post: "Devuelto para corrección", approve: "Revisión aprobada",
  send: "Envío registrado", set_second: "Segundo inspector actualizado",
  set_entity: "Entidad actualizada",
};
const requestTypes = { inicial: "Autorización inicial", renovacion: "Renovación" };
let month = new Date();
month = new Date(month.getFullYear(), month.getMonth(), 1);
let loadRevision = 0;
let data = null;
let selectedId = null;
let planning = null;
let planningRevision = 0;
const planningStatus = {
  vencida: ["Vencida", "workflow-deadline-overdue"],
  proxima: ["Próxima (90 días)", "workflow-deadline-warning"],
  al_dia: ["Al día", "workflow-deadline-on-time"],
  sin_fecha: ["Sin próxima fecha", "planning-status-none"],
};

function renderCalendar() {
  byId("inspection-month").textContent = new Intl.DateTimeFormat("es-UY", {
    month: "long", year: "numeric",
  }).format(month);
  const firstDay = (month.getDay() + 6) % 7;
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const body = byId("inspection-calendar-days");
  body.replaceChildren();
  for (let week = 0; week < Math.ceil((firstDay + days) / 7); week++) {
    const row = element("tr");
    for (let weekday = 0; weekday < 7; weekday++) {
      const day = week * 7 + weekday - firstDay + 1;
      const cell = element("td");
      if (day > 0 && day <= days) {
        cell.append(element("span", String(day)));
        const key = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        for (const inspection of data?.inspections || []) {
          if (inspection.scheduled_date !== key) continue;
          const button = element("button", `${inspection.scheduled_time} · ${inspection.company_name || inspection.case_number || `#${inspection.id}`}`, "inspection-calendar-event");
          button.type = "button";
          const people = [inspection.inspector_name, inspection.second_inspector_name].filter(Boolean).join(" y ");
          button.title = `${inspection.scheduled_place} · ${people}`;
          button.addEventListener("click", () => selectInspection(inspection.id));
          cell.append(button);
        }
      }
      row.append(cell);
    }
    body.append(row);
  }
}

function inputLabel(text, input) {
  const label = element("label", text);
  label.append(input);
  return label;
}

function checklist(name, values, editable) {
  const fieldset = element("fieldset", undefined, "inspection-checklist");
  fieldset.append(element("legend", name));
  for (let i = 0; i < 6; i++) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = `check-${i}`;
    input.checked = values[i] === true;
    input.disabled = !editable;
    const label = element("label");
    label.append(input, document.createTextNode(` Ítem ${i + 1}`));
    fieldset.append(label);
  }
  return fieldset;
}

function isChief(user) {
  return user.role === "admin" || user.workflow_role === "jefe_inspeccion";
}

function inspectorSelect(name, placeholderText, selected = null, required = false) {
  const select = document.createElement("select");
  select.name = name;
  select.required = required;
  fillInspectorSelect(select, placeholderText, selected);
  return select;
}

function fillInspectorSelect(select, placeholderText, selected = null) {
  const current = selected ?? (select.value ? Number(select.value) : null);
  const placeholder = element("option", data?.inspectors?.length ? placeholderText : "No hay inspectores activos");
  placeholder.value = "";
  select.replaceChildren(placeholder);
  for (const person of data?.inspectors || []) {
    const option = element("option", person.display_name);
    option.value = String(person.id);
    option.selected = person.id === current;
    select.append(option);
  }
}

let entityNames = null;
let entityRequest = null;

function entityKey(value) {
  return String(value || "").normalize("NFKC").trim().toLocaleLowerCase("es");
}

// Lista de entidades usuarias (carpetas de «3 Entidades Usuarias») para sugerir al escribir.
function loadEntityOptions() {
  if (entityNames) return Promise.resolve(entityNames);
  entityRequest ||= requestJson("/api/entities?all=1").then(result => {
    entityNames = result.items.map(item => item.name);
    byId("inspection-entity-options").replaceChildren(...entityNames.map(name => {
      const option = document.createElement("option");
      option.value = name;
      return option;
    }));
    return entityNames;
  }).catch(error => {
    entityRequest = null;
    setMessage("inspection-message", `No se pudo cargar la lista de entidades: ${error.message}`, "error");
    return [];
  });
  return entityRequest;
}

function matchingEntity(name) {
  const key = entityKey(name);
  return key ? (entityNames || []).find(entity => entityKey(entity) === key) || "" : "";
}

// Al elegir la entidad en el formulario de creación se completan los datos vacíos con su ficha.
async function prefillFromEntity(form) {
  const name = form.elements.entity_name.value.trim();
  if (!name) return;
  await loadEntityOptions();
  const entity = matchingEntity(name);
  if (!entity) return;
  try {
    const record = await requestJson(`/api/entity/record?name=${encodeURIComponent(entity)}`);
    if (form.elements.entity_name.value.trim() !== name) return;
    let filled = 0;
    for (const field of ["rut", "phone", "email", "contact_details", "address", "city"]) {
      if (!form.elements[field].value && record.profile[field]) {
        form.elements[field].value = record.profile[field];
        filled += 1;
      }
    }
    if (filled) setMessage("inspection-message", `Se completaron ${filled} dato(s) con la ficha de ${entity}.`, "success");
  } catch {
    /* la ficha es opcional: si falla, el formulario sigue igual */
  }
}

function optionalId(value) {
  return value ? Number(value) : null;
}

function actionButton(label, action, className = "button workflow-approve") {
  const button = element("button", label, className);
  button.type = "submit";
  button.dataset.action = action;
  return button;
}

async function submitAction(form, inspection, action) {
  const payload = { inspection_id: inspection.id, version: inspection.version, action };
  if (action === "assign") payload.inspector_id = Number(form.elements.inspector_id.value);
  if (action === "assign" || action === "set_second") {
    payload.second_inspector_id = optionalId(form.elements.second_inspector_id.value);
  }
  if (action === "complete_pre" || action === "complete_post") {
    payload.checklist = [...form.querySelectorAll('input[type="checkbox"]')].map(input => input.checked);
  }
  if (action === "complete_pre") {
    payload.scheduled_date = form.elements.scheduled_date.value;
    payload.scheduled_time = form.elements.scheduled_time.value;
    payload.scheduled_place = form.elements.scheduled_place.value;
  }
  if (action === "approve" || action === "return_post") payload.comment = form.elements.comment.value;
  if (action === "set_entity") payload.entity_name = form.elements.entity_name.value;
  const buttons = [...form.querySelectorAll("button")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    await requestJson("/api/inspections/action", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (await loadInspections()) setMessage("inspection-message", "Acción registrada.", "success");
  } catch (error) {
    setMessage("inspection-message", error.message, "error");
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

function renderActions(detail, inspection) {
  const user = data.current_user;
  const chief = isChief(user);
  const inspector = user.role === "admin" ||
    (user.workflow_role === "inspeccion" && user.id === inspection.inspector_id);
  if (user.role !== "admin" && user.id === inspection.second_inspector_id && user.id !== inspection.inspector_id) {
    detail.append(element("p", "Eres el segundo inspector: puedes ver esta inspección y su agenda, pero el checklist y el envío los gestiona el inspector líder.", "form-help"));
  }
  if (inspection.entity_name) {
    const open = element("button", `Ver entidad: ${inspection.entity_name}`, "button secondary small");
    open.type = "button";
    open.addEventListener("click", () => window.dispatchEvent(new CustomEvent("workflow-open-linked-profile", {
      detail: { kind: "entities", name: inspection.entity_name },
    })));
    detail.append(open);
  }
  if (data.can_create) {
    const entityForm = element("form", undefined, "workflow-form inspection-action-form");
    const input = document.createElement("input");
    input.name = "entity_name";
    input.maxLength = 255;
    input.autocomplete = "off";
    input.placeholder = "Escribe para buscar la entidad (vacío para quitarla)";
    input.setAttribute("list", "inspection-entity-options");
    input.value = inspection.entity_name || "";
    input.addEventListener("focus", loadEntityOptions, { once: true });
    entityForm.append(inputLabel("Entidad usuaria", input),
      actionButton(inspection.entity_name ? "Cambiar entidad" : "Asociar entidad", "set_entity", "button secondary"));
    entityForm.addEventListener("submit", event => {
      event.preventDefault();
      submitAction(entityForm, inspection, event.submitter.dataset.action);
    });
    detail.append(entityForm);
  }
  if (chief && !["assignment", "completed"].includes(inspection.stage)) {
    const secondForm = element("form", undefined, "workflow-form inspection-action-form");
    secondForm.append(
      inputLabel("Segundo inspector", inspectorSelect("second_inspector_id", "Sin segundo inspector", inspection.second_inspector_id)),
      actionButton("Guardar segundo inspector", "set_second", "button secondary"),
    );
    secondForm.addEventListener("submit", event => {
      event.preventDefault();
      submitAction(secondForm, inspection, event.submitter.dataset.action);
    });
    detail.append(secondForm);
  }
  const form = element("form", undefined, "workflow-form inspection-action-form");
  if (inspection.stage === "assignment" && chief) {
    form.append(
      inputLabel("Inspector líder", inspectorSelect("inspector_id", "Selecciona un inspector", null, true)),
      inputLabel("Segundo inspector (opcional)", inspectorSelect("second_inspector_id", "Sin segundo inspector")),
      actionButton("Asignar y enviar al inspector", "assign"),
    );
  } else if (inspection.stage === "pre_inspection" && inspector) {
    form.append(checklist("Checklist previo", inspection.pre_checklist, true));
    for (const [name, label, type, value] of [
      ["scheduled_date", "Fecha de inspección", "date", inspection.scheduled_date],
      ["scheduled_time", "Hora de inspección", "time", inspection.scheduled_time],
      ["scheduled_place", "Lugar de inspección", "text", inspection.scheduled_place],
    ]) {
      const input = document.createElement("input");
      input.name = name;
      input.type = type;
      input.required = true;
      input.value = value;
      if (name === "scheduled_place") input.maxLength = 255;
      form.append(inputLabel(label, input));
    }
    form.append(actionButton("Completar checklist y agendar", "complete_pre"));
  } else if (inspection.stage === "post_inspection" && inspector) {
    form.append(checklist("Checklist posterior", inspection.post_checklist, true),
      actionButton("Enviar checklist al jefe", "complete_post"));
  } else if (inspection.stage === "chief_review" && chief) {
    const comment = document.createElement("textarea");
    comment.name = "comment";
    comment.maxLength = 1000;
    comment.rows = 3;
    form.append(inputLabel("Observaciones (obligatorias al devolver)", comment),
      actionButton("Aprobar y enviar al inspector", "approve"),
      actionButton("Devolver para corrección", "return_post", "button workflow-observe"));
  } else if (inspection.stage === "send" && inspector) {
    form.append(actionButton("Registrar envío", "send"));
  } else {
    return;
  }
  form.addEventListener("submit", event => {
    event.preventDefault();
    submitAction(form, inspection, event.submitter.dataset.action);
  });
  detail.append(form);
}

function selectInspection(id) {
  selectedId = id;
  const inspection = data.inspections.find(item => item.id === id);
  if (!inspection) return;
  for (const row of byId("inspection-rows").querySelectorAll("[data-inspection-id]")) {
    const selected = row.dataset.inspectionId === String(id);
    row.classList.toggle("selected", selected);
    row.querySelector("button").setAttribute("aria-pressed", String(selected));
  }
  const detail = byId("inspection-detail");
  detail.classList.remove("empty-state");
  const summary = element("div", undefined, "workflow-case-summary");
  summary.append(element("h3", inspection.company_name || `Inspección #${inspection.id}`),
    element("p", `Inspección #${inspection.id} · ${stages[inspection.stage] || inspection.stage}`));
  const fields = element("section", undefined, "workflow-application-data");
  fields.append(element("h4", "Datos de la inspección"));
  const list = document.createElement("dl");
  for (const [field, label] of Object.entries(labels)) {
    const value = field === "request_type" ? requestTypes[inspection[field]] : inspection[field];
    const item = document.createElement("div");
    item.append(element("dt", label), element("dd", value || "—"));
    list.append(item);
  }
  fields.append(list);
  detail.replaceChildren(summary, fields);
  if (inspection.pre_checklist.length) detail.append(checklist("Checklist previo", inspection.pre_checklist, false));
  if (inspection.post_checklist.length && inspection.stage !== "post_inspection") {
    detail.append(checklist("Checklist posterior", inspection.post_checklist, false));
  }
  renderActions(detail, inspection);
  if (inspection.events.length) {
    const history = element("section", undefined, "workflow-history");
    history.append(element("h4", "Historial"));
    const entries = document.createElement("ol");
    for (const event of inspection.events) {
      const when = new Date(event.created_at).toLocaleString("es-UY");
      const item = element("li");
      item.append(element("strong", event.actor_name), ` · ${eventLabels[event.action] || event.action} · ${when}`);
      if (event.comment) item.append(element("p", event.comment));
      if (event.details_text) item.append(element("p", event.details_text, "form-help"));
      entries.append(item);
    }
    history.append(entries);
    detail.append(history);
  }
}

function renderInspections(inspections) {
  const rows = byId("inspection-rows");
  rows.replaceChildren();
  if (!inspections.length) {
    const row = element("tr");
    const cell = element("td", "No hay inspecciones para mostrar.");
    cell.colSpan = 6;
    row.append(cell);
    rows.append(row);
    return;
  }
  for (const inspection of inspections) {
    const row = element("tr");
    row.className = "workflow-case-row";
    row.dataset.inspectionId = String(inspection.id);
    const first = element("td");
    const button = element("button", inspection.company_name || inspection.case_number || `Inspección #${inspection.id}`, "button secondary small inspection-row-title");
    button.type = "button";
    button.title = `Inspección #${inspection.id}${inspection.case_number ? ` · Trámite ${inspection.case_number}` : ""}`;
    button.setAttribute("aria-pressed", String(inspection.id === selectedId));
    row.classList.toggle("selected", inspection.id === selectedId);
    row.addEventListener("click", () => selectInspection(inspection.id));
    first.append(button);
    if (inspection.case_number && inspection.company_name) {
      first.append(element("span", `Trámite ${inspection.case_number}`, "inspection-row-case"));
    }
    row.append(first,
      element("td", requestTypes[inspection.request_type] || "—"),
      element("td", inspection.practice || "—"),
      element("td", inspection.inspector_name || "Sin asignar"),
      element("td", inspection.second_inspector_name || "—"),
      element("td", stages[inspection.stage] || inspection.stage));
    rows.append(row);
  }
}

function renderCreateAssignment() {
  const chief = data.can_create && isChief(data.current_user);
  const lead = byId("inspection-lead-select");
  const second = byId("inspection-second-select");
  byId("inspection-lead-field").classList.toggle("hidden", !chief);
  byId("inspection-second-field").classList.toggle("hidden", !chief);
  lead.disabled = !chief;
  second.disabled = !chief;
  lead.required = chief && data.current_user.workflow_role === "jefe_inspeccion";
  fillInspectorSelect(lead, "Selecciona el inspector líder");
  fillInspectorSelect(second, "Sin segundo inspector");
  byId("inspection-create-help").textContent = chief
    ? "Los datos son opcionales. Elige el inspector líder; el segundo inspector solo verá la inspección agendada."
    : "Los datos son opcionales. Jefe de Inspección asignará al inspector.";
}

function formatDate(value) {
  if (!value) return "—";
  const [year, month, day] = value.split("-");
  return `${day}/${month}/${year}`;
}

function normalizeText(value) {
  return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

const monthNames = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
let editingId = null;
let pickerYear = null;

function monthLabel(value) {
  const [year, month] = value.split("-");
  return `${monthNames[Number(month) - 1]} de ${year}`;
}

function planningFilters() {
  return {
    q: byId("planning-query").value.trim(),
    status: byId("planning-status").value,
    month: byId("planning-month").value,
    category: byId("planning-category").value,
    sort: byId("planning-sort").value,
  };
}

function fillPlanningSelects() {
  const category = byId("planning-category");
  const currentCategory = category.value;
  const categories = [...new Set(planning.items.flatMap(item => item.categories))].sort((a, b) => a.localeCompare(b, "es"));
  category.replaceChildren(Object.assign(element("option", "Todas"), { value: "" }),
    ...categories.map(name => Object.assign(element("option", name), { value: name, selected: name === currentCategory })));
  renderMonthPicker();
}

function monthKey(year, month) {
  return `${year}-${String(month).padStart(2, "0")}`;
}

function currentMonthKey(offset = 0) {
  const [year, month] = planning.today.split("-").map(Number);
  const date = new Date(year, month - 1 + offset, 1);
  return monthKey(date.getFullYear(), date.getMonth() + 1);
}

function renderMonthPicker() {
  if (!planning) return;
  if (pickerYear === null) pickerYear = Number(planning.today.slice(0, 4));
  const counts = new Map();
  for (const item of planning.items) {
    if (item.next_inspection) {
      const key = item.next_inspection.slice(0, 7);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }
  const selected = byId("planning-month").value;
  const thisMonth = currentMonthKey();
  let total = 0;
  const grid = byId("planning-month-grid");
  grid.replaceChildren();
  for (let month = 1; month <= 12; month++) {
    const key = monthKey(pickerYear, month);
    const count = counts.get(key) || 0;
    total += count;
    const button = element("button", undefined, "planning-month");
    button.type = "button";
    button.classList.toggle("empty", !count);
    button.classList.toggle("past", key < thisMonth);
    button.classList.toggle("current", key === thisMonth);
    button.setAttribute("aria-pressed", String(key === selected));
    button.setAttribute("aria-label", `${monthLabel(key)}: ${count} ${count === 1 ? "vencimiento" : "vencimientos"}`);
    button.append(element("small", monthNames[month - 1]), element("strong", String(count)));
    button.addEventListener("click", () => selectPlanningMonth(key === selected ? "" : key));
    grid.append(button);
  }
  byId("planning-year").textContent = `${pickerYear} · ${total} ${total === 1 ? "vencimiento" : "vencimientos"}`;
  renderMonthSelection();
}

function renderMonthSelection() {
  const key = byId("planning-month").value;
  byId("planning-month-selected").textContent = key ? `· ${monthLabel(key)}` : "· cualquier mes";
}

function setCalendarVisible(visible) {
  byId("planning-calendar-body").classList.toggle("hidden", !visible);
  const toggle = byId("planning-calendar-toggle");
  toggle.textContent = visible ? "Ocultar calendario" : "Mostrar calendario";
  toggle.setAttribute("aria-expanded", String(visible));
  try { localStorage.setItem("planning-calendar-hidden", visible ? "0" : "1"); } catch { /* sin almacenamiento */ }
}

function selectPlanningMonth(key) {
  byId("planning-month").value = key;
  if (key) pickerYear = Number(key.slice(0, 4));
  renderMonthPicker();
  renderPlanning();
}

function renderPlanning() {
  const rows = byId("planning-rows");
  rows.replaceChildren();
  if (!planning) return;
  const filters = planningFilters();
  const query = normalizeText(filters.q);
  const text = (a, b) => a.localeCompare(b, "es", { sensitivity: "base" });
  const sorters = {
    next: (a, b) => (a.next_inspection || "9999").localeCompare(b.next_inspection || "9999") || text(a.institution, b.institution),
    last: (a, b) => (b.last_inspection || "").localeCompare(a.last_inspection || "") || text(a.institution, b.institution),
    institution: (a, b) => text(a.institution, b.institution) || text(a.practice, b.practice),
    practice: (a, b) => text(a.practice, b.practice) || text(a.institution, b.institution),
  };
  const items = planning.items.filter(item =>
    (!filters.status || item.status === filters.status) &&
    (!filters.month || (item.next_inspection || "").startsWith(`${filters.month}-`)) &&
    (!filters.category || item.categories.includes(filters.category)) &&
    (!query || normalizeText(`${item.institution} ${item.practice} ${item.department}`).includes(query)))
    .sort(sorters[filters.sort]);
  byId("planning-export").href = "/api/inspections/planning/export?" + new URLSearchParams(filters);
  const counts = { vencida: 0, proxima: 0, al_dia: 0, sin_fecha: 0 };
  for (const item of planning.items) counts[item.status] += 1;
  const sync = planning.sync || { files: [], errors: [], root_exists: true };
  const syncedAt = sync.synced_at ? new Date(sync.synced_at).toLocaleString("es-UY") : "nunca";
  byId("planning-summary").textContent =
    `${items.length} de ${planning.items.length} filas` +
    (filters.month ? ` vencen en ${monthLabel(filters.month)}` : "") +
    ` · ${counts.vencida} vencidas · ${counts.proxima} en los próximos 90 días · ${counts.al_dia} al día · ` +
    `${counts.sin_fecha} sin próxima fecha. Planillas leídas el ${syncedAt}` +
    (sync.synced_by_name ? ` por ${sync.synced_by_name}` : "") +
    `: ${sync.files.map(file => file.file).join(", ") || "ninguna"}.` +
    (sync.errors.length ? ` No se pudieron leer: ${sync.errors.map(item => item.file).join(", ")} (las filas de esas planillas se conservan).` : "") +
    (sync.root_exists ? "" : ` No se encontró la carpeta ${sync.root}.`);
  if (!items.length) {
    const row = element("tr");
    const cell = element("td", "No hay datos para mostrar.");
    cell.colSpan = 9;
    row.append(cell);
    rows.append(row);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const row = element("tr");
    const [label, className] = planningStatus[item.status] || [item.status, "planning-status-none"];
    const statusCell = element("td");
    statusCell.append(element("span", label, `workflow-deadline-badge ${className}`));
    const institutionCell = element("td", item.institution);
    if (item.entity_name) institutionCell.append(element("span", `Entidad: ${item.entity_name}`, "inspection-row-case"));
    if (item.edited.length) institutionCell.append(element("span", "Corregido a mano", "planning-flag"));
    if (!item.in_sheets) institutionCell.append(element("span", "Ya no figura en las planillas", "planning-flag"));
    if (item.notes) institutionCell.title = item.notes;
    const sourceCell = element("td", item.categories.join(", "));
    sourceCell.title = item.sources.join("\n");
    const actions = element("td");
    const box = element("div", undefined, "planning-actions");
    const schedule = element("button", "Agendar inspección", "button small workflow-approve");
    schedule.type = "button";
    schedule.addEventListener("click", () => scheduleFromPlanning(item));
    const edit = element("button", "Editar", "button secondary small");
    edit.type = "button";
    edit.addEventListener("click", () => openPlanningEditor(item.id));
    box.append(schedule, edit);
    if (item.inspections.length) {
      box.append(element("span", `Agendada: ${item.inspections.map(entry => `#${entry.id}`).join(", ")}`, "form-help"));
    }
    actions.append(box);
    row.append(institutionCell, element("td", item.practice || "—"), element("td", item.department || "—"),
      element("td", formatDate(item.last_inspection)), element("td", formatDate(item.next_inspection)),
      statusCell, element("td", String(item.equipment_count)), sourceCell, actions);
    fragment.append(row);
  }
  rows.append(fragment);
}

function openPlanningEditor(id) {
  const item = planning?.items.find(entry => entry.id === id);
  const form = byId("planning-editor");
  if (!item) {
    editingId = null;
    form.classList.add("hidden");
    return;
  }
  editingId = id;
  byId("planning-editor-title").textContent = `Corregir: ${item.institution} · ${item.practice || "sin práctica"}`;
  for (const field of ["institution", "practice", "department", "last_inspection", "next_inspection", "notes", "entity_name"]) {
    form.elements[field].value = item[field] || "";
  }
  const sheet = item.sheet;
  byId("planning-editor-sheet").textContent = item.in_sheets
    ? `Según las planillas: ${sheet.institution} · ${sheet.practice || "—"} · ${sheet.department || "—"} · ` +
      `última ${formatDate(sheet.last_inspection)} · próxima ${formatDate(sheet.next_inspection)}.`
    : "Esta fila ya no figura en las planillas; se conserva con sus datos y correcciones.";
  byId("planning-editor-restore").disabled = !item.edited.length;
  const history = byId("planning-editor-history");
  history.replaceChildren(...item.events.map(event => {
    const entry = element("li");
    entry.append(element("strong", event.actor_name),
      ` · ${event.action} · ${new Date(event.created_at).toLocaleString("es-UY")}${event.details_text ? ` · ${event.details_text}` : ""}`);
    return entry;
  }));
  form.classList.remove("hidden");
  form.scrollIntoView({ block: "nearest" });
}

async function savePlanningItem(payload) {
  const item = planning.items.find(entry => entry.id === editingId);
  if (!item) return;
  const buttons = [...byId("planning-editor").querySelectorAll("button")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    planning = await requestJson("/api/inspections/planning/update", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: item.id, version: item.version, ...payload }),
    });
    fillPlanningSelects();
    renderPlanning();
    openPlanningEditor(item.id);
    setMessage("inspection-message", payload.restore ? "Datos de las planillas restaurados." : "Corrección guardada.", "success");
  } catch (error) {
    setMessage("inspection-message", error.message, "error");
  } finally {
    buttons.forEach(button => { button.disabled = false; });
    const current = planning.items.find(entry => entry.id === editingId);
    byId("planning-editor-restore").disabled = !current?.edited.length;
  }
}

function scheduleFromPlanning(item) {
  showInspectionView("list");
  const panel = byId("inspection-create-panel");
  const form = byId("inspection-form");
  form.reset();
  renderCreateAssignment();
  form.elements.company_name.value = item.institution;
  form.elements.practice.value = item.practice;
  form.elements.city.value = item.department;
  form.elements.planning_item_id.value = String(item.id);
  form.elements.entity_name.value = item.entity_name || "";
  if (!item.entity_name) {
    // Si hay una entidad con el mismo nombre que la institución, se sugiere sola.
    loadEntityOptions().then(() => {
      if (!form.elements.entity_name.value && form.elements.planning_item_id.value === String(item.id)) {
        form.elements.entity_name.value = matchingEntity(item.institution);
      }
      prefillFromEntity(form);
    });
  } else {
    prefillFromEntity(form);
  }
  panel.open = true;
  panel.scrollIntoView({ block: "start" });
  form.elements.company_name.focus();
  setMessage("inspection-message", `Datos precargados desde la planificación: ${item.institution} · ${item.practice}. Completa el resto y elige al inspector líder.`, "success");
}

async function loadPlanning(sync = false) {
  const revision = ++planningRevision;
  byId("planning-summary").textContent = sync ? "Leyendo planillas…" : "Cargando planificación…";
  try {
    const result = await requestJson(sync ? "/api/inspections/planning/sync" : "/api/inspections/planning",
      sync ? { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" } : undefined);
    if (revision !== planningRevision) return;
    planning = result;
    fillPlanningSelects();
    renderPlanning();
    if (editingId !== null) openPlanningEditor(editingId);
    if (sync) setMessage("inspection-message", "Planillas leídas. Las correcciones manuales se conservaron.", "success");
  } catch (error) {
    if (revision === planningRevision) {
      byId("planning-summary").textContent = "";
      setMessage("inspection-message", error.message, "error");
    }
  }
}

function showInspectionView(view) {
  const chief = data && isChief(data.current_user);
  const planningView = chief && view === "planning";
  byId("inspection-main").classList.toggle("hidden", planningView);
  byId("inspection-planning").classList.toggle("hidden", !planningView);
  for (const button of byId("inspection-subtabs").querySelectorAll("[data-inspection-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.inspectionView === (planningView ? "planning" : "list")));
  }
  if (planningView && !planning) loadPlanning();
}

export async function loadInspections() {
  const revision = ++loadRevision;
  try {
    const result = await requestJson("/api/inspections");
    if (revision !== loadRevision) return false;
    data = result;
    byId("inspection-create-panel").classList.toggle("hidden", !data.can_create);
    renderCreateAssignment();
    const chiefView = isChief(data.current_user);
    byId("inspection-subtabs").classList.toggle("hidden", !chiefView);
    if (!chiefView) showInspectionView("list");
    renderInspections(data.inspections);
    renderCalendar();
    if (data.inspections.some(item => item.id === selectedId)) {
      selectInspection(selectedId);
    } else {
      selectedId = null;
      const detail = byId("inspection-detail");
      detail.classList.add("empty-state");
      detail.textContent = "Selecciona una inspección para ver sus datos.";
    }
    setMessage("inspection-message");
    return true;
  } catch (error) {
    if (revision === loadRevision) setMessage("inspection-message", error.message, "error");
    return false;
  }
}

export function initInspections() {
  renderCalendar();
  for (const button of byId("inspection-subtabs").querySelectorAll("[data-inspection-view]")) {
    button.addEventListener("click", () => showInspectionView(button.dataset.inspectionView));
  }
  byId("inspection-planning-filter").addEventListener("submit", event => event.preventDefault());
  byId("planning-year-prev").addEventListener("click", () => { if (planning) { pickerYear -= 1; renderMonthPicker(); } });
  byId("planning-year-next").addEventListener("click", () => { if (planning) { pickerYear += 1; renderMonthPicker(); } });
  byId("planning-month-this").addEventListener("click", () => planning && selectPlanningMonth(currentMonthKey()));
  byId("planning-month-next").addEventListener("click", () => planning && selectPlanningMonth(currentMonthKey(1)));
  byId("planning-month-clear").addEventListener("click", () => selectPlanningMonth(""));
  byId("planning-calendar-toggle").addEventListener("click", () =>
    setCalendarVisible(byId("planning-calendar-body").classList.contains("hidden")));
  let calendarHidden = false;
  try { calendarHidden = localStorage.getItem("planning-calendar-hidden") === "1"; } catch { /* sin almacenamiento */ }
  setCalendarVisible(!calendarHidden);
  renderMonthSelection();
  for (const id of ["planning-query", "planning-status", "planning-category", "planning-sort"]) {
    byId(id).addEventListener(id === "planning-query" ? "input" : "change", renderPlanning);
  }
  byId("planning-reload").addEventListener("click", () => loadPlanning(true));
  for (const input of document.querySelectorAll("[data-entity-input]")) {
    input.addEventListener("focus", loadEntityOptions);
  }
  const createForm = byId("inspection-form");
  createForm.elements.entity_name.addEventListener("change", () => prefillFromEntity(createForm));
  byId("inspection-create-panel").addEventListener("toggle", event => {
    // Al cerrar el formulario se olvida la fila de planificación precargada.
    if (!event.currentTarget.open) byId("inspection-planning-item").value = "";
  });
  const editor = byId("planning-editor");
  editor.addEventListener("submit", event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(editor));
    savePlanningItem(values);
  });
  byId("planning-editor-restore").addEventListener("click", () => savePlanningItem({ restore: true }));
  byId("planning-editor-cancel").addEventListener("click", () => openPlanningEditor(null));
  byId("inspection-prev-month").addEventListener("click", () => {
    month = new Date(month.getFullYear(), month.getMonth() - 1, 1);
    renderCalendar();
  });
  byId("inspection-next-month").addEventListener("click", () => {
    month = new Date(month.getFullYear(), month.getMonth() + 1, 1);
    renderCalendar();
  });
  byId("inspection-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form));
    for (const name of ["inspector_id", "second_inspector_id", "planning_item_id"]) {
      if (name in payload) payload[name] = optionalId(payload[name]);
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const created = await requestJson("/api/inspections", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      selectedId = created.id;
      const fromPlanning = Boolean(payload.planning_item_id);
      form.reset();
      form.elements.planning_item_id.value = "";
      if (fromPlanning) planning = null;
      byId("inspection-create-panel").open = false;
      const text = created.stage === "pre_inspection"
        ? "Inspección creada y asignada al inspector líder."
        : "Inspección creada y enviada a Jefe de Inspección.";
      if (await loadInspections()) setMessage("inspection-message", text, "success");
    } catch (error) {
      setMessage("inspection-message", error.message, "error");
    } finally {
      submit.disabled = false;
    }
  });
}
