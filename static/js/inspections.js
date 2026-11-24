import { element, requestJson, setMessage } from "./common.js";

const byId = id => document.getElementById(id);
const labels = {
  application_date: "Fecha de solicitud", case_number: "Número de trámite",
  company_name: "Institución o empresa", rut: "RUT", request_type: "Tipo de solicitud",
  inspection_type: "Tipo de inspección", practice: "Práctica",
  equipment_model: "Modelo de equipo a inspeccionar", contact_details: "Datos de contacto",
  phone: "Teléfono", address: "Domicilio", city: "Ciudad", email: "Email",
  inspector_name: "Inspector líder", scheduled_date: "Fecha de inspección",
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
  send: "Envío registrado",
};
const requestTypes = { inicial: "Autorización inicial", renovacion: "Renovación" };
let month = new Date();
month = new Date(month.getFullYear(), month.getMonth(), 1);
let loadRevision = 0;
let data = null;
let selectedId = null;

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
          button.title = `${inspection.scheduled_place} · ${inspection.inspector_name}`;
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

function actionButton(label, action, className = "button workflow-approve") {
  const button = element("button", label, className);
  button.type = "submit";
  button.dataset.action = action;
  return button;
}

async function submitAction(form, inspection, action) {
  const payload = { inspection_id: inspection.id, version: inspection.version, action };
  if (action === "assign") payload.inspector_id = Number(form.elements.inspector_id.value);
  if (action === "complete_pre" || action === "complete_post") {
    payload.checklist = [...form.querySelectorAll('input[type="checkbox"]')].map(input => input.checked);
  }
  if (action === "complete_pre") {
    payload.scheduled_date = form.elements.scheduled_date.value;
    payload.scheduled_time = form.elements.scheduled_time.value;
    payload.scheduled_place = form.elements.scheduled_place.value;
  }
  if (action === "approve" || action === "return_post") payload.comment = form.elements.comment.value;
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
  const chief = user.role === "admin" || user.workflow_role === "jefe_inspeccion";
  const inspector = user.role === "admin" ||
    (user.workflow_role === "inspeccion" && user.id === inspection.inspector_id);
  const form = element("form", undefined, "workflow-form inspection-action-form");
  if (inspection.stage === "assignment" && chief) {
    const select = document.createElement("select");
    select.name = "inspector_id";
    select.required = true;
    const placeholder = element("option", data.inspectors.length ? "Selecciona un inspector" : "No hay inspectores activos");
    placeholder.value = "";
    select.append(placeholder);
    for (const person of data.inspectors) {
      const option = element("option", person.display_name);
      option.value = String(person.id);
      select.append(option);
    }
    form.append(inputLabel("Inspector líder", select), actionButton("Asignar y enviar al inspector", "assign"));
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
    cell.colSpan = 4;
    row.append(cell);
    rows.append(row);
    return;
  }
  for (const inspection of inspections) {
    const row = element("tr");
    row.className = "workflow-case-row";
    row.dataset.inspectionId = String(inspection.id);
    const first = element("td");
    const button = element("button", inspection.case_number || `Inspección #${inspection.id}`, "button secondary small");
    button.type = "button";
    button.setAttribute("aria-pressed", String(inspection.id === selectedId));
    row.classList.toggle("selected", inspection.id === selectedId);
    row.addEventListener("click", () => selectInspection(inspection.id));
    first.append(button);
    row.append(first, element("td", inspection.company_name || "—"),
      element("td", inspection.inspector_name || "Sin asignar"), element("td", stages[inspection.stage] || inspection.stage));
    rows.append(row);
  }
}

export async function loadInspections() {
  const revision = ++loadRevision;
  try {
    const result = await requestJson("/api/inspections");
    if (revision !== loadRevision) return false;
    data = result;
    byId("inspection-create-panel").classList.toggle("hidden", !data.can_create);
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
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const created = await requestJson("/api/inspections", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      selectedId = created.id;
      form.reset();
      byId("inspection-create-panel").open = false;
      if (await loadInspections()) setMessage("inspection-message", "Inspección creada y enviada a Jefe de Inspección.", "success");
    } catch (error) {
      setMessage("inspection-message", error.message, "error");
    } finally {
      submit.disabled = false;
    }
  });
}
