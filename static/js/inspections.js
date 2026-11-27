import { element, historyItem, personChip, requestJson, setMessage } from "./common.js";

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
  set_entity: "Entidad actualizada", save_checklist: "Checklist guardado",
  coordinate_driver: "Chofer coordinado",
  reschedule: "Inspección re-coordinada",
  add_requirement: "Requerimiento agregado",
  remove_requirement: "Requerimiento eliminado",
  resolve_requirement: "Requerimiento resuelto",
  reopen_requirement: "Requerimiento reabierto",
  extend_requirement: "Plazo de requerimiento extendido",
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

function isoDay(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

// Iniciales para el distintivo redondo de cada inspector.
function personCell(name, emptyText) {
  const cell = element("td");
  if (!name) {
    cell.append(element("span", emptyText, "inspection-person-empty"));
    return cell;
  }
  cell.append(personChip(name));
  return cell;
}

function stageBadge(stage) {
  const badge = element("span", stages[stage] || stage, "inspection-stage-badge");
  badge.dataset.stage = stage;
  return badge;
}

// Inspección de otro inspector: solo se muestran los datos de agenda.
function showCalendarOnly(inspection) {
  selectedId = null;
  renderCalendar();
  for (const row of byId("inspection-rows").querySelectorAll("[data-inspection-id]")) {
    row.classList.remove("selected");
    row.querySelector("button")?.setAttribute("aria-pressed", "false");
  }
  const detail = byId("inspection-detail");
  detail.classList.remove("empty-state");
  const summary = element("div", undefined, "workflow-case-summary");
  const subtitle = element("p", `Inspección #${inspection.id}`, "workflow-summary-subtitle");
  subtitle.append(stageBadge(inspection.stage));
  summary.append(element("h3", inspection.company_name || inspection.case_number || `Inspección #${inspection.id}`), subtitle);
  const fields = element("section", undefined, "workflow-application-data");
  fields.append(element("h4", "Agenda (inspección de otro inspector)"));
  const list = document.createElement("dl");
  for (const [field, label] of [
    ["scheduled_date", "Fecha de inspección"], ["scheduled_time", "Hora de inspección"],
    ["scheduled_place", "Lugar de inspección"], ["inspector_name", "Inspector líder"],
    ["second_inspector_name", "Segundo inspector"],
  ]) {
    const item = document.createElement("div");
    item.append(element("dt", label), element("dd", inspection[field] || "—"));
    list.append(item);
  }
  fields.append(list);
  detail.replaceChildren(summary, fields);
}

let calendarMode = "inspections";

function formatDay(iso) {
  const [year, monthNumber, day] = (iso || "").split("-");
  return year ? `${day}/${monthNumber}/${year}` : "—";
}

async function requirementAction(inspection, payload) {
  await checklistSaves;
  try {
    await requestJson("/api/inspections/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ inspection_id: inspection.id, version: inspection.version, ...payload }),
    });
    if (await loadInspections()) {
      setMessage("inspection-message", payload.action === "add_requirement"
        ? "Requerimiento agregado." : "Requerimiento eliminado.", "success");
    }
  } catch (error) {
    setMessage("inspection-message", error.message, "error");
  }
}

// Requerimientos de la inspección: lista y, si se puede editar, alta de a uno con texto y plazo.
function requirementsSection(inspection, editable) {
  const section = element("fieldset", undefined, "inspection-checklist inspection-requirements");
  section.append(element("legend", "Requerimientos"));
  const items = inspection.requirements || [];
  if (!items.length) section.append(element("p", "Sin requerimientos.", "form-help"));
  for (const item of items) {
    const row = element("div", undefined, "inspection-requirement");
    const open = element("button", item.text, "inspection-requirement-link");
    open.type = "button";
    open.title = "Ver en el calendario de requerimientos";
    open.addEventListener("click", () => openRequirementInCalendar(item));
    row.append(open,
      element("strong", item.resolved_at ? "Resuelto" : `Plazo: ${formatDay(item.due_date)}`));
    if (item.resolved_at) row.classList.add("resolved");
    if (editable) {
      const remove = element("button", "Quitar", "button secondary small");
      remove.type = "button";
      remove.addEventListener("click", () => requirementAction(inspection,
        { action: "remove_requirement", requirement_id: item.id }));
      row.append(remove);
    }
    section.append(row);
  }
  if (editable) {
    const text = document.createElement("textarea");
    text.rows = 2;
    text.maxLength = 1000;
    text.placeholder = "Descripción del requerimiento";
    const due = document.createElement("input");
    due.type = "date";
    const add = element("button", "Agregar requerimiento", "button secondary");
    add.type = "button";
    add.addEventListener("click", () => {
      if (!text.value.trim() || !due.value) {
        setMessage("inspection-message", "Escribe el requerimiento y elige su plazo.", "error");
        return;
      }
      requirementAction(inspection, { action: "add_requirement", requirement_text: text.value, due_date: due.value });
    });
    // Están dentro del formulario del checklist: Enter agrega el requerimiento en vez de enviarlo.
    due.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        add.click();
      }
    });
    section.append(inputLabel("Nuevo requerimiento", text), inputLabel("Plazo", due), add);
  }
  return section;
}

async function requirementUpdate(requirement, payload, message) {
  try {
    await requestJson("/api/inspections/requirement", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requirement_id: requirement.id, ...payload }),
    });
    if (await loadInspections()) {
      const updated = data.requirements.find(item => item.id === requirement.id);
      if (updated) showRequirement(updated);
      setMessage("inspection-message", message, "success");
    }
  } catch (error) {
    setMessage("inspection-message", error.message, "error");
  }
}

// Detalle de un requerimiento abierto desde el calendario, con resolver y extender plazo.
let selectedRequirementId = null;

// Lleva el calendario al mes del plazo, lo muestra en modo requerimientos y abre la ficha.
function openRequirementInCalendar(requirement) {
  const [year, monthNumber] = requirement.due_date.split("-").map(Number);
  month = new Date(year, monthNumber - 1, 1);
  calendarMode = "requirements";
  const current = data.requirements.find(item => item.id === requirement.id) || requirement;
  showRequirement(current);
  const target = byId("inspection-calendar-days")
    .querySelector(`[data-requirement-id="${requirement.id}"]`);
  (target || byId("inspection-calendar-title")).scrollIntoView({ behavior: "smooth", block: "center" });
  target?.focus({ preventScroll: true });
}

function showRequirement(requirement) {
  selectedId = null;
  selectedRequirementId = requirement.id;
  renderCalendar();
  const detail = byId("inspection-detail");
  detail.classList.remove("empty-state");
  const summary = element("div", undefined, "workflow-case-summary");
  summary.append(element("h3", "Requerimiento"),
    element("p", requirement.resolved_at ? "Resuelto" : `Plazo: ${formatDay(requirement.due_date)}`,
      "workflow-summary-subtitle"));
  const fields = element("section", undefined, "workflow-application-data");
  const list = document.createElement("dl");
  for (const [label, value] of [
    ["Requerimiento", requirement.text],
    ["Plazo", formatDay(requirement.due_date)],
    ["Inspección", requirement.company_name || requirement.case_number || `#${requirement.inspection_id}`],
    ["Inspector líder", requirement.inspector_name],
    ["Ingresado por", requirement.created_by_name],
    ["Estado", requirement.resolved_at ? "Resuelto" : "Pendiente"],
  ]) {
    const item = document.createElement("div");
    item.append(element("dt", label), element("dd", value || "—"));
    list.append(item);
  }
  fields.append(list);
  detail.replaceChildren(summary, fields);
  const actions = element("div", undefined, "workflow-form inspection-action-form");
  if (requirement.resolved_at) {
    const reopen = element("button", "Reabrir requerimiento", "button secondary");
    reopen.type = "button";
    reopen.addEventListener("click", () => requirementUpdate(requirement, { action: "reopen" }, "Requerimiento reabierto."));
    actions.append(reopen);
  } else {
    const resolve = element("button", "Resolver requerimiento", "button workflow-approve");
    resolve.type = "button";
    resolve.addEventListener("click", () => requirementUpdate(requirement, { action: "resolve" }, "Requerimiento resuelto."));
    const due = document.createElement("input");
    due.type = "date";
    due.value = requirement.due_date;
    due.min = requirement.due_date;
    const extend = element("button", "Extender plazo", "button secondary");
    extend.type = "button";
    extend.addEventListener("click", () => {
      if (!due.value || due.value <= requirement.due_date) {
        setMessage("inspection-message", "Elige un plazo posterior al actual.", "error");
        return;
      }
      requirementUpdate(requirement, { action: "extend", due_date: due.value }, "Plazo extendido.");
    });
    actions.append(resolve, inputLabel("Nuevo plazo", due), extend);
  }
  if (data.inspections.some(item => item.id === requirement.inspection_id)) {
    const open = element("button", "Ver inspección", "button secondary small");
    open.type = "button";
    open.addEventListener("click", () => selectInspection(requirement.inspection_id));
    actions.append(open);
  }
  detail.append(actions);
}

function calendarEvents(key) {
  if (calendarMode === "requirements") {
    return (data?.requirements || []).filter(item => item.due_date === key).map(item => ({
      id: item.inspection_id, time: item.resolved_at ? "✓ Resuelto" : "Plazo", title: item.text,
      stage: item.resolved_at ? "requirement-resolved" : "requirement",
      tooltip: `${item.company_name || item.case_number || `Inspección #${item.inspection_id}`} · ${item.inspector_name || ""}`,
      source: item,
    }));
  }
  return (data?.calendar || data?.inspections || [])
    .filter(inspection => inspection.scheduled_date === key)
    .sort((a, b) => (a.scheduled_time || "").localeCompare(b.scheduled_time || ""))
    .map(inspection => ({
      id: inspection.id, time: inspection.scheduled_time || "",
      title: inspection.company_name || inspection.case_number || `#${inspection.id}`, stage: inspection.stage,
      tooltip: `${inspection.scheduled_place} · ${[inspection.inspector_name, inspection.second_inspector_name].filter(Boolean).join(" y ")}`,
      source: inspection,
    }));
}

function renderCalendar() {
  byId("inspection-calendar-title").textContent = calendarMode === "requirements"
    ? "Calendario de requerimientos" : "Calendario de inspecciones";
  for (const button of byId("inspection-calendar-mode").querySelectorAll("[data-calendar-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.calendarMode === calendarMode));
  }
  byId("inspection-month").textContent = new Intl.DateTimeFormat("es-UY", {
    month: "long", year: "numeric",
  }).format(month);
  const today = isoDay(new Date());
  const firstDay = (month.getDay() + 6) % 7;
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const body = byId("inspection-calendar-days");
  body.replaceChildren();
  for (let week = 0; week < Math.ceil((firstDay + days) / 7); week++) {
    const row = element("tr");
    for (let weekday = 0; weekday < 7; weekday++) {
      const day = week * 7 + weekday - firstDay + 1;
      const cell = element("td");
      if (weekday >= 5) cell.classList.add("weekend");
      if (day > 0 && day <= days) {
        const key = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        cell.append(element("span", String(day), "inspection-calendar-day"));
        if (key === today) cell.classList.add("today");
        for (const event of calendarEvents(key)) {
          const button = element("button", undefined, "inspection-calendar-event");
          button.append(element("strong", event.time), element("span", event.title));
          button.dataset.stage = event.stage;
          button.classList.toggle("selected", calendarMode === "requirements"
            ? event.source.id === selectedRequirementId : event.id === selectedId);
          if (calendarMode === "requirements") button.dataset.requirementId = String(event.source.id);
          button.type = "button";
          button.title = event.tooltip;
          const own = data.inspections.some(item => item.id === event.id);
          if (!own && calendarMode === "inspections") button.classList.add("other-inspector");
          button.addEventListener("click", () => {
            if (calendarMode === "requirements") showRequirement(event.source);
            else if (own) selectInspection(event.id);
            else showCalendarOnly(event.source);
          });
          cell.append(button);
        }
      } else {
        cell.classList.add("outside");
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

// Guarda las casillas en el servidor al marcarlas, en orden, para que no se pierdan al recargar.
const preChecklistItems = [
  "Coordinar inspección con el usuario",
  "Locomoción",
  "Preparar documentación",
];
const locomotionOptions = { auto_miem: "Auto MIEM", taxi: "Taxi", propios: "Medios propios" };
const driverStatus = { pending: "Pendiente de coordinar", coordinated: "Chofer coordinado" };

function isCoordinator(user) {
  return user.role !== "admin" && user.workflow_role === "coordinacion";
}

function driverText(inspection) {
  if (inspection.driver_status === "coordinated") {
    return `Chofer coordinado${inspection.driver_name ? `: ${inspection.driver_name}` : ""}`;
  }
  return driverStatus[inspection.driver_status] || "";
}

function travelText(inspection) {
  const parts = [];
  if (inspection.departure_time) parts.push(`salida ${inspection.departure_time}`);
  if (inspection.arrival_time) parts.push(`llegada aprox. ${inspection.arrival_time}`);
  return parts.length ? `Auto MIEM: ${parts.join(", ")}` : "Auto MIEM: horarios sin completar";
}

function inspectionWhen(inspection) {
  const when = [inspection.scheduled_date, inspection.scheduled_time].filter(Boolean).join(" ");
  return [when, inspection.scheduled_place].filter(Boolean).join(" · ") || "sin fecha agendada";
}

// Tareas de Coordinación (chofer por coordinar) y confirmaciones que recibe el inspector.
function renderDriverTasks() {
  const panel = byId("inspection-driver-tasks");
  const user = data.current_user;
  let title = "";
  let items = [];
  if (isCoordinator(user)) {
    title = "Tareas: coordinar con chofer";
    items = data.inspections.filter(item => item.driver_status === "pending" && item.stage !== "completed");
  } else {
    title = "Choferes coordinados para tus inspecciones";
    items = data.inspections.filter(item => item.driver_status === "coordinated" &&
      item.stage !== "completed" && item.inspector_id === user.id);
  }
  panel.replaceChildren();
  panel.classList.toggle("hidden", !items.length);
  if (!items.length) return;
  panel.append(element("h3", title));
  const list = element("ul");
  for (const item of items) {
    const entry = element("li");
    const open = element("button", item.company_name || `Inspección #${item.id}`, "button secondary small");
    open.type = "button";
    open.addEventListener("click", () => selectInspection(item.id));
    const extra = isCoordinator(user)
      ? ` ${inspectionWhen(item)} · ${travelText(item)} · Inspector: ${item.inspector_name || "—"}` +
        (item.driver_name ? ` · Re-coordinada (chofer anterior: ${item.driver_name})` : "")
      : ` ${inspectionWhen(item)} · ${driverText(item)}`;
    entry.append(open, document.createTextNode(extra));
    list.append(entry);
  }
  panel.append(list);
}
const postChecklistItems = [
  "Escanear documentación y guardarla en formato digital y físico en la carpeta correspondiente",
  "Enviar por mail el acta de inspección a Secretaría",
  "Ingreso en ARIS (solo equipos nuevos)",
];
let checklistSaves = Promise.resolve();

function autosaveChecklist(fieldset, inspection, key) {
  fieldset.addEventListener("change", () => {
    const values = [...fieldset.querySelectorAll('input[name^="check-"]')].map(input => input.checked);
    const locomotion = fieldset.querySelector('select[name="locomotion"]');
    const arisNa = fieldset.querySelector('input[name="aris_not_applicable"]');
    const nextDate = fieldset.querySelector('input[name="next_inspection_date"]');
    const mailSaved = fieldset.querySelector('input[name="send_mail_saved"]');
    const travel = locomotion && locomotion.value === "auto_miem"
      ? {
        departure_time: fieldset.querySelector('input[name="departure_time"]').value,
        arrival_time: fieldset.querySelector('input[name="arrival_time"]').value,
      }
      : { departure_time: "", arrival_time: "" };
    checklistSaves = checklistSaves.then(async () => {
      try {
        const payload = {
          inspection_id: inspection.id, version: inspection.version,
          action: "save_checklist",
        };
        if (key !== "send") payload.checklist = values;
        if (mailSaved) payload.send_mail_saved = mailSaved.checked;
        if (locomotion) Object.assign(payload, { locomotion: locomotion.value }, travel);
        if (arisNa) payload.aris_not_applicable = arisNa.checked;
        if (nextDate) payload.next_inspection_date = nextDate.value;
        const result = await requestJson("/api/inspections/action", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        inspection.version = result.version;
        if (key !== "send") inspection[key] = values;
        if (mailSaved) inspection.send_mail_saved = mailSaved.checked ? 1 : 0;
        if (arisNa) inspection.aris_not_applicable = arisNa.checked ? 1 : 0;
        if (nextDate) inspection.next_inspection_date = nextDate.value;
        // Al empezar el checklist posterior ya no se puede re-coordinar.
        if (key === "post_checklist" && postChecklistStarted(inspection)) byId("inspection-reschedule")?.remove();
        if (locomotion) {
          inspection.locomotion = locomotion.value;
          Object.assign(inspection, travel);
          if (locomotion.value !== "auto_miem") {
            inspection.driver_status = "";
            inspection.driver_name = "";
          } else if (!inspection.driver_status) {
            inspection.driver_status = "pending";
          }
        }
        setMessage("inspection-message", "Checklist guardado.", "success");
      } catch (error) {
        setMessage("inspection-message", error.message, "error");
      }
    });
  });
}

function locomotionSelect(value, editable) {
  const select = document.createElement("select");
  select.name = "locomotion";
  select.disabled = !editable;
  select.setAttribute("aria-label", "Medio de locomoción");
  select.append(Object.assign(element("option", "Selecciona el medio"), { value: "" }));
  for (const [key, text] of Object.entries(locomotionOptions)) {
    select.append(Object.assign(element("option", text), { value: key, selected: key === value }));
  }
  return select;
}

function travelTimes(inspection, editable, select) {
  const wrapper = element("span", undefined, "inspection-travel-times");
  for (const [name, text] of [["departure_time", "Hora de salida"], ["arrival_time", "Hora aprox. de llegada"]]) {
    const input = document.createElement("input");
    input.type = "time";
    input.name = name;
    input.value = inspection[name] || "";
    input.disabled = !editable;
    const label = element("label", `${text} `);
    label.append(input);
    wrapper.append(label);
  }
  const toggle = () => wrapper.classList.toggle("hidden", select.value !== "auto_miem");
  select.addEventListener("change", toggle);
  toggle();
  return wrapper;
}

function checklist(name, values, editable, items = null, locomotion = "", inspection = null) {
  const fieldset = element("fieldset", undefined, "inspection-checklist");
  fieldset.append(element("legend", name));
  // Un checklist guardado con el formato anterior (6 casillas) se muestra con sus rótulos
  // genéricos; si hay que volver a completarlo (por ejemplo, devuelto por el jefe), empieza vacío.
  const mismatch = Boolean(items && values.length && values.length !== items.length);
  const shown = mismatch && editable ? [] : values;
  const legacy = !items || (mismatch && !editable);
  const labels = legacy ? Array.from({ length: values.length || 6 }, (_, i) => `Ítem ${i + 1}`) : items;
  labels.forEach((text, i) => {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = `check-${i}`;
    input.checked = shown[i] === true;
    input.disabled = !editable;
    const label = element("label");
    label.append(input, document.createTextNode(` ${text}`));
    if (!legacy && items === preChecklistItems && text === "Locomoción") {
      const select = locomotionSelect(locomotion, editable);
      label.append(document.createTextNode(" "), select);
      fieldset.append(label);
      if (inspection) fieldset.append(travelTimes(inspection, editable, select));
      return;
    }
    if (!legacy && items === postChecklistItems && i === 2) {
      const na = document.createElement("input");
      na.type = "checkbox";
      na.name = "aris_not_applicable";
      na.checked = Boolean(inspection?.aris_not_applicable);
      na.disabled = !editable;
      const naLabel = element("label", undefined, "inspection-checklist-na");
      naLabel.append(na, document.createTextNode(" No aplica (no es un equipo nuevo)"));
      fieldset.append(label, naLabel);
      return;
    }
    fieldset.append(label);
  });
  return fieldset;
}

function sendChecklistFieldset(inspection, editable) {
  const fieldset = element("fieldset", undefined, "inspection-checklist");
  fieldset.append(element("legend", "Checklist de envío"));
  const mail = document.createElement("input");
  mail.type = "checkbox";
  mail.name = "send_mail_saved";
  mail.checked = Boolean(inspection.send_mail_saved);
  mail.disabled = !editable;
  const mailLabel = element("label");
  mailLabel.append(mail, document.createTextNode(" Mail de envío guardado en línea de tiempo"));
  const next = document.createElement("input");
  next.type = "date";
  next.name = "next_inspection_date";
  next.value = inspection.next_inspection_date || "";
  next.disabled = !editable;
  const nextLabel = element("label", "Fecha de la próxima inspección ", "inspection-next-date");
  nextLabel.append(next);
  fieldset.append(mailLabel, nextLabel);
  if (editable) {
    fieldset.append(element("p", inspection.planning_item_id
      ? "Esta fecha actualiza la próxima inspección en la planificación."
      : "Esta inspección no está vinculada a la planificación: la fecha queda solo en la inspección.",
    "form-help"));
  }
  return fieldset;
}

function postChecklist(inspection, editable) {
  return checklist("Checklist posterior", inspection.post_checklist, editable, postChecklistItems, "", inspection);
}

function preChecklist(inspection, editable) {
  return checklist("Checklist previo", inspection.pre_checklist, editable, preChecklistItems,
    inspection.locomotion || "", inspection);
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

// Datos que se completaron desde la ficha de una entidad, para quitarlos si se cambia de entidad.
const prefilledFields = new WeakMap();
const PREFILL_FIELDS = ["rut", "phone", "email", "contact_details", "address", "city"];

function clearPrefilled(form) {
  const filled = prefilledFields.get(form) || {};
  for (const [field, value] of Object.entries(filled)) {
    // Solo se borra si nadie lo cambió a mano después de completarlo.
    if (form.elements[field].value === value) form.elements[field].value = "";
  }
  prefilledFields.delete(form);
}

// Al elegir la entidad en el formulario de creación se completan los datos vacíos con su ficha.
// intro: aviso previo (por ejemplo, el de la planificación) que no debe perderse.
async function prefillFromEntity(form, intro = "") {
  clearPrefilled(form);
  const name = form.elements.entity_name.value.trim();
  if (!name) return;
  await loadEntityOptions();
  const entity = matchingEntity(name);
  if (!entity) return;
  try {
    const record = await requestJson(`/api/entity/record?name=${encodeURIComponent(entity)}`);
    if (form.elements.entity_name.value.trim() !== name) return;
    const filled = {};
    for (const field of PREFILL_FIELDS) {
      if (!form.elements[field].value && record.profile[field]) {
        form.elements[field].value = record.profile[field];
        filled[field] = record.profile[field];
      }
    }
    prefilledFields.set(form, filled);
    const count = Object.keys(filled).length;
    if (count) {
      setMessage("inspection-message",
        `${intro ? `${intro} ` : ""}Se completaron ${count} dato(s) con la ficha de ${entity}.`, "success");
    }
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

function missingPreChecklist(form) {
  const missing = [...form.querySelectorAll('input[name^="check-"]')]
    .map((input, i) => input.checked ? null : `marcar «${preChecklistItems[i]}»`).filter(Boolean);
  if (!form.elements.locomotion.value) missing.push("elegir el medio de locomoción");
  return missing;
}

async function submitAction(form, inspection, action) {
  await checklistSaves;
  if (action === "complete_post") {
    const done = [...form.querySelectorAll('input[name^="check-"]')].map(input => input.checked);
    done[2] = done[2] || form.elements.aris_not_applicable.checked;
    const missing = postChecklistItems.filter((_, i) => !done[i]).map(name => `marcar «${name}»`);
    if (missing.length) {
      setMessage("inspection-message", `Para enviar al jefe falta: ${missing.join("; ")}.`, "error");
      return;
    }
  }
  if (action === "send") {
    const missing = [];
    if (!form.elements.send_mail_saved.checked) missing.push("marcar «Mail de envío guardado en línea de tiempo»");
    if (!form.elements.next_inspection_date.value) missing.push("cargar la fecha de la próxima inspección");
    if (missing.length) {
      setMessage("inspection-message", `Para registrar el envío falta: ${missing.join("; ")}.`, "error");
      return;
    }
  }
  if (action === "complete_pre") {
    const missing = missingPreChecklist(form);
    if (missing.length) {
      setMessage("inspection-message", `Para agendar falta: ${missing.join("; ")}.`, "error");
      return;
    }
  }
  const payload = { inspection_id: inspection.id, version: inspection.version, action };
  if (action === "assign") payload.inspector_id = Number(form.elements.inspector_id.value);
  if (action === "assign" || action === "set_second") {
    payload.second_inspector_id = optionalId(form.elements.second_inspector_id.value);
  }
  if (action === "complete_pre" || action === "complete_post") {
    payload.checklist = [...form.querySelectorAll('input[name^="check-"]')].map(input => input.checked);
  }
  if (action === "complete_post") payload.aris_not_applicable = form.elements.aris_not_applicable.checked;
  if (action === "send") {
    payload.send_mail_saved = form.elements.send_mail_saved.checked;
    payload.next_inspection_date = form.elements.next_inspection_date.value;
  }
  if (action === "complete_pre") {
    payload.locomotion = form.elements.locomotion.value;
    const auto = payload.locomotion === "auto_miem";
    payload.departure_time = auto ? form.elements.departure_time.value : "";
    payload.arrival_time = auto ? form.elements.arrival_time.value : "";
  }
  if (action === "complete_pre") {
    payload.scheduled_date = form.elements.scheduled_date.value;
    payload.scheduled_time = form.elements.scheduled_time.value;
    payload.scheduled_place = form.elements.scheduled_place.value;
  }
  if (action === "approve" || action === "return_post") payload.comment = form.elements.comment.value;
  if (action === "set_entity") payload.entity_name = form.elements.entity_name.value;
  if (action === "coordinate_driver") payload.driver_name = form.elements.driver_name.value;
  if (action === "reschedule") {
    for (const name of ["scheduled_date", "scheduled_time", "scheduled_place", "comment"]) {
      payload[name] = form.elements[name].value;
    }
    if (form.elements.departure_time) {
      payload.departure_time = form.elements.departure_time.value;
      payload.arrival_time = form.elements.arrival_time.value;
    }
  }
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

function postChecklistStarted(inspection) {
  return inspection.post_checklist.some(Boolean) || Boolean(inspection.aris_not_applicable) ||
    Boolean(inspection.requirements?.length);
}

function rescheduleForm(inspection) {
  const panel = element("details", undefined, "workflow-create-panel");
  panel.id = "inspection-reschedule";
  panel.append(element("summary", "Re-coordinar inspección"));
  const form = element("form", undefined, "workflow-form inspection-action-form");
  const fields = [
    ["scheduled_date", "Nueva fecha", "date", inspection.scheduled_date, true],
    ["scheduled_time", "Nueva hora", "time", inspection.scheduled_time, true],
    ["scheduled_place", "Lugar", "text", inspection.scheduled_place, true],
  ];
  if (inspection.locomotion === "auto_miem") {
    fields.push(["departure_time", "Hora de salida (Auto MIEM)", "time", inspection.departure_time, false],
      ["arrival_time", "Hora aprox. de llegada", "time", inspection.arrival_time, false]);
  }
  for (const [name, label, type, value, required] of fields) {
    const input = document.createElement("input");
    input.name = name;
    input.type = type;
    input.required = required;
    input.value = value || "";
    if (name === "scheduled_place") input.maxLength = 255;
    form.append(inputLabel(label, input));
  }
  const reason = document.createElement("textarea");
  reason.name = "comment";
  reason.maxLength = 1000;
  reason.rows = 2;
  form.append(inputLabel("Motivo (opcional)", reason));
  if (inspection.locomotion === "auto_miem") {
    form.append(element("p", "Coordinación recibirá de nuevo la tarea de coordinar el chofer.", "form-help"));
  }
  form.append(actionButton("Guardar nueva coordinación", "reschedule"));
  form.addEventListener("submit", event => {
    event.preventDefault();
    submitAction(form, inspection, event.submitter.dataset.action);
  });
  panel.append(form);
  return panel;
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
  if (isCoordinator(user) || (user.role === "admin" && inspection.driver_status)) {
    if (["pending", "coordinated"].includes(inspection.driver_status)) {
      const driverForm = element("form", undefined, "workflow-form inspection-action-form");
      const input = document.createElement("input");
      input.name = "driver_name";
      input.maxLength = 120;
      input.required = true;
      input.value = inspection.driver_name || "";
      input.placeholder = "Nombre del chofer";
      driverForm.append(inputLabel("Chofer que viene", input),
        actionButton(inspection.driver_status === "coordinated" ? "Cambiar chofer" : "Confirmar chofer", "coordinate_driver"));
      driverForm.addEventListener("submit", event => {
        event.preventDefault();
        submitAction(driverForm, inspection, event.submitter.dataset.action);
      });
      detail.append(driverForm);
    }
    if (isCoordinator(user)) return;
  }
  if (inspector && inspection.stage === "post_inspection" && !postChecklistStarted(inspection)) {
    detail.append(rescheduleForm(inspection));
  }
  const form = element("form", undefined, "workflow-form inspection-action-form");
  if (inspection.stage === "assignment" && chief) {
    form.append(
      inputLabel("Inspector líder", inspectorSelect("inspector_id", "Selecciona un inspector", null, true)),
      inputLabel("Segundo inspector (opcional)", inspectorSelect("second_inspector_id", "Sin segundo inspector")),
      actionButton("Asignar y enviar al inspector", "assign"),
    );
  } else if (inspection.stage === "pre_inspection" && inspector) {
    const pre = preChecklist(inspection, true);
    autosaveChecklist(pre, inspection, "pre_checklist");
    form.append(pre);
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
    const post = postChecklist(inspection, true);
    autosaveChecklist(post, inspection, "post_checklist");
    form.append(post, requirementsSection(inspection, true), actionButton("Enviar checklist al jefe", "complete_post"));
  } else if (inspection.stage === "chief_review" && chief) {
    const comment = document.createElement("textarea");
    comment.name = "comment";
    comment.maxLength = 1000;
    comment.rows = 3;
    form.append(inputLabel("Observaciones (obligatorias al devolver)", comment),
      actionButton("Aprobar y enviar al inspector", "approve"),
      actionButton("Devolver para corrección", "return_post", "button workflow-observe"));
  } else if (inspection.stage === "send" && inspector) {
    const sendChecklist = sendChecklistFieldset(inspection, true);
    autosaveChecklist(sendChecklist, inspection, "send");
    form.append(sendChecklist, actionButton("Registrar envío", "send"));
  } else {
    return;
  }
  form.addEventListener("submit", event => {
    event.preventDefault();
    submitAction(form, inspection, event.submitter.dataset.action);
  });
  detail.append(form);
}

function isLeadInspector(inspection) {
  const user = data.current_user;
  return user.role === "admin" || (user.workflow_role === "inspeccion" && user.id === inspection.inspector_id);
}

function selectInspection(id) {
  selectedId = id;
  selectedRequirementId = null;
  const inspection = data.inspections.find(item => item.id === id);
  if (!inspection) return;
  for (const row of byId("inspection-rows").querySelectorAll("[data-inspection-id]")) {
    const selected = row.dataset.inspectionId === String(id);
    row.classList.toggle("selected", selected);
    row.querySelector("button").setAttribute("aria-pressed", String(selected));
  }
  renderCalendar();
  const detail = byId("inspection-detail");
  detail.classList.remove("empty-state");
  const summary = element("div", undefined, "workflow-case-summary");
  const subtitle = element("p", `Inspección #${inspection.id}`, "workflow-summary-subtitle");
  subtitle.append(stageBadge(inspection.stage));
  summary.append(element("h3", inspection.company_name || `Inspección #${inspection.id}`), subtitle);
  const fields = element("section", undefined, "workflow-application-data");
  fields.append(element("h4", "Datos de la inspección"));
  const list = document.createElement("dl");
  for (const [field, label] of Object.entries(labels)) {
    const value = field === "request_type" ? requestTypes[inspection[field]] : inspection[field];
    const item = document.createElement("div");
    item.append(element("dt", label), element("dd", value || "—"));
    list.append(item);
  }
  if (inspection.driver_status) {
    const item = document.createElement("div");
    item.append(element("dt", "Chofer (Auto MIEM)"), element("dd", driverText(inspection)));
    list.append(item);
    for (const [field, label] of [["departure_time", "Hora de salida"], ["arrival_time", "Hora aprox. de llegada"]]) {
      const time = document.createElement("div");
      time.append(element("dt", label), element("dd", inspection[field] || "—"));
      list.append(time);
    }
  }
  fields.append(list);
  detail.replaceChildren(summary, fields);
  if (inspection.pre_checklist.length) detail.append(preChecklist(inspection, false));
  if (inspection.post_checklist.length && inspection.stage !== "post_inspection") {
    detail.append(postChecklist(inspection, false));
  }
  if (inspection.stage === "completed" && (inspection.send_mail_saved || inspection.next_inspection_date)) {
    detail.append(sendChecklistFieldset(inspection, false));
  }
  if (inspection.requirements?.length && (inspection.stage !== "post_inspection" || !isLeadInspector(inspection))) {
    detail.append(requirementsSection(inspection, false));
  }
  renderActions(detail, inspection);
  if (inspection.events.length) {
    const history = element("section", undefined, "workflow-history");
    history.append(element("h4", "Historial"));
    const entries = document.createElement("ol");
    for (const event of inspection.events) {
      const when = new Date(event.created_at).toLocaleString("es-UY", { dateStyle: "short", timeStyle: "short" });
      entries.append(historyItem(event.actor_name, `· ${eventLabels[event.action] || event.action}`, when, [
        [event.comment], [event.details_text, "workflow-history-detail"],
      ]));
    }
    history.append(entries);
    detail.append(history);
  }
}

let listFilter = "active";

// Una inspección finalizada con requerimientos sin resolver va a su propia sub pestaña;
// cuando se resuelven todos, vuelve a "Finalizadas".
function listGroup(inspection) {
  if (inspection.stage !== "completed") return "active";
  return inspection.pending_requirements > 0 ? "completed_pending" : "completed";
}

const searchFields = [
  "company_name", "rut", "practice", "inspector_name", "second_inspector_name", "case_number",
  "entity_name", "city", "address", "equipment_model", "inspection_type", "contact_details",
  "email", "phone", "scheduled_place",
];

function normalizeSearch(value) {
  return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

// Cada palabra buscada tiene que aparecer en algún dato de la inspección.
function matchesSearch(inspection, words) {
  const text = normalizeSearch([
    ...searchFields.map(field => inspection[field]),
    `#${inspection.id}`, stages[inspection.stage], requestTypes[inspection.request_type],
  ].join(" "));
  return words.every(word => text.includes(word));
}

function renderInspectionList() {
  const words = normalizeSearch(byId("inspection-search").value).split(/\s+/).filter(Boolean);
  const searching = words.length > 0;
  byId("inspection-search-note").classList.toggle("hidden", !searching);
  for (const button of byId("inspection-list-filter").querySelectorAll("[data-inspection-filter]")) {
    button.setAttribute("aria-pressed", String(!searching && button.dataset.inspectionFilter === listFilter));
  }
  renderInspections(data.inspections.filter(item =>
    searching ? matchesSearch(item, words) : listGroup(item) === listFilter));
}

function renderInspections(inspections) {
  const rows = byId("inspection-rows");
  rows.replaceChildren();
  const count = byId("inspection-count");
  if (count) count.textContent = String(inspections.length);
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
    const button = element("button", inspection.company_name || inspection.case_number || `Inspección #${inspection.id}`, "inspection-row-title");
    button.type = "button";
    button.title = `Inspección #${inspection.id}${inspection.case_number ? ` · Trámite ${inspection.case_number}` : ""}`;
    button.setAttribute("aria-pressed", String(inspection.id === selectedId));
    row.classList.toggle("selected", inspection.id === selectedId);
    row.addEventListener("click", () => selectInspection(inspection.id));
    first.append(button);
    if (inspection.case_number && inspection.company_name) {
      first.append(element("span", `Trámite ${inspection.case_number}`, "inspection-row-case"));
    }
    const requestCell = element("td");
    if (requestTypes[inspection.request_type]) {
      const tag = element("span", requestTypes[inspection.request_type], "inspection-request-tag");
      tag.dataset.type = inspection.request_type;
      requestCell.append(tag);
    } else {
      requestCell.append(element("span", "—", "inspection-person-empty"));
    }
    const stageCell = element("td");
    stageCell.append(stageBadge(inspection.stage));
    row.append(first, requestCell,
      element("td", inspection.practice || "—"),
      personCell(inspection.inspector_name, "Sin asignar"),
      personCell(inspection.second_inspector_name, "—"),
      stageCell);
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
  prefilledFields.delete(form);
  form.elements.company_name.value = item.institution;
  form.elements.practice.value = item.practice;
  // El departamento de la planilla no es la ciudad: al guardar, va al Departamento de la ficha.
  form.elements.planning_item_id.value = String(item.id);
  form.elements.entity_name.value = item.entity_name || "";
  const intro = `Datos precargados desde la planificación: ${item.institution} · ${item.practice}. ` +
    "Completa el resto y elige al inspector líder.";
  setMessage("inspection-message", intro, "success");
  if (!item.entity_name) {
    // Si hay una entidad con el mismo nombre que la institución, se sugiere sola.
    loadEntityOptions().then(() => {
      if (!form.elements.entity_name.value && form.elements.planning_item_id.value === String(item.id)) {
        form.elements.entity_name.value = matchingEntity(item.institution);
      }
      prefillFromEntity(form, intro);
    });
  } else {
    prefillFromEntity(form, intro);
  }
  panel.open = true;
  panel.scrollIntoView({ block: "start" });
  form.elements.company_name.focus();
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
    renderInspectionList();
    renderDriverTasks();
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
  for (const button of byId("inspection-calendar-mode").querySelectorAll("[data-calendar-mode]")) {
    button.addEventListener("click", () => {
      calendarMode = button.dataset.calendarMode;
      renderCalendar();
    });
  }
  for (const button of byId("inspection-list-filter").querySelectorAll("[data-inspection-filter]")) {
    button.addEventListener("click", () => {
      listFilter = button.dataset.inspectionFilter;
      byId("inspection-search").value = "";
      if (data) renderInspectionList();
    });
  }
  byId("inspection-search").addEventListener("input", () => {
    if (data) renderInspectionList();
  });
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
  byId("inspection-today").addEventListener("click", () => {
    const now = new Date();
    month = new Date(now.getFullYear(), now.getMonth(), 1);
    renderCalendar();
  });
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
      prefilledFields.delete(form);
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
