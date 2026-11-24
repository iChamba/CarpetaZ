import { element, makeButton, requestJson, setMessage } from "./common.js";

let data;
let selectedCaseId = null;
let selectedCaseGroup = null;
let linkedUserOffset = 0;
let linkedUserTotal = 0;
let linkedUserRequest = 0;
let linkedUsers = new Map();
let selectedLinkedUser = "";
let selectedAuthorizationPath = "";
let linkedEntityRequest = 0;
let selectedLinkedEntity = "";
let workflowRevision = 0;
let refreshingCaseList = false;
const linkedUserPageSize = 100;
const requestTypeLabels = {
  nueva: "Nueva",
  renovacion: "Renovación",
  modificacion: "Modificación",
};
const deliveryMethodLabels = {
  presencial: "Entregado presencial",
  apia: "Entregado por APIA",
  mail: "Entregado por mail",
};
const roleLabels = {
  administracion: "Secretaría",
  inspeccion: "Inspección",
  jefe_inspeccion: "Jefe de Inspección",
  jefe_licenciamiento: "Jefe de Licenciamiento",
  abogado: "Abogado",
  director: "Director",
};
const applicationFieldLabels = {
  application_date: "Fecha de solicitud",
  form_number: "Número de formulario",
  short_code: "Código corto",
  first_name: "Nombre",
  last_name: "Apellido",
  identity_number: "CI",
  profession: "Profesión",
  practice: "Práctica",
  workplace: "Lugar de trabajo",
  email: "Correo",
  assigned_code: "Código asignado",
  digital_signature: "Firma digital",
};
const entityApplicationFieldLabels = {
  application_date: "Fecha de solicitud",
  form_number: "Número de formulario",
  company_name: "Empresa/institución",
  practice: "Práctica",
  code: "Código",
  digital_signature: "Firma digital",
};

const byId = id => document.getElementById(id);

function formatDate(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString("es-UY", { dateStyle: "short", timeStyle: "short" });
}

function formatDuration(seconds) {
  const totalSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(totalSeconds / 60);
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  const remainingSeconds = totalSeconds % 60;
  if (hours) return `${hours} h ${remainingMinutes} min`;
  if (minutes) return `${minutes} min ${remainingSeconds} s`;
  return `${remainingSeconds} s`;
}

function formatDeadlineDuration(minutes) {
  const total = Math.max(0, minutes);
  const days = Math.floor(total / 1440);
  const hours = Math.floor(total % 1440 / 60);
  return `${days} d ${hours} h ${total % 60} min`;
}

export function workflowDeadline(item, now = Date.now()) {
  const days = item.workflow_name === "Autorización Individual" ? 30 : 60;
  const warningDays = days === 30 ? 5 : 10;
  const created = Date.parse(item.created_at);
  const started = Date.parse(item.deadline_started_at);
  const finalTime = item.status === "completed" ? Date.parse(item.completed_at) : now;
  const paused = item.deadline_paused_at == null ? null : Date.parse(item.deadline_paused_at);
  if (!Number.isFinite(created) || !Number.isFinite(started) || !Number.isFinite(finalTime)
      || started < created || finalTime < started
      || (paused !== null && (!Number.isFinite(paused) || paused < started || paused > finalTime))) {
    return { days, dueAt: null, text: "Plazo no disponible: fecha inválida", tone: "unavailable" };
  }
  const end = paused === null ? finalTime : paused;
  const dueAt = started + days * 86400 * 1000;
  const remaining = dueAt - end;
  const overdue = remaining <= 0;
  const tone = overdue ? "overdue" : remaining <= warningDays * 86400 * 1000 ? "warning" : "on-time";
  let text;
  if (item.status === "completed") {
    const elapsed = formatDeadlineDuration(Math.floor((end - started) / 60000));
    text = `Tiempo total: ${elapsed} · ${overdue ? "Fuera de plazo" : "Dentro de plazo"}`;
  } else {
    const duration = formatDeadlineDuration(overdue ? Math.floor(-remaining / 60000) : Math.ceil(remaining / 60000));
    text = `${paused === null ? "" : "Pausado · "}${overdue ? `Vencido: ${duration}` : `Restan: ${duration}`}`;
  }
  return { days, dueAt: paused === null ? dueAt : null, text, tone };
}

function updateDeadlineDisplay(target, item, detailed = false) {
  const deadline = data?.supports_observation_deadline === true
    ? workflowDeadline(item)
    : { days: 0, dueAt: null, text: "Reinicia la aplicación para actualizar el plazo.", tone: "unavailable" };
  const text = detailed && deadline.dueAt !== null
    ? `Plazo: ${deadline.days} días corridos · Vence: ${formatDate(deadline.dueAt)} · ${deadline.text}`
    : detailed && deadline.text.startsWith("Pausado")
    ? `Plazo: ${deadline.days} días corridos · ${deadline.text}` : deadline.text;
  const icon = element("span", "\u23f1", "workflow-deadline-icon");
  icon.setAttribute("aria-hidden", "true");
  target.replaceChildren(icon, document.createTextNode(text));
  for (const tone of ["on-time", "warning", "overdue", "unavailable"]) {
    target.classList.toggle(`workflow-deadline-${tone}`, deadline.tone === tone);
  }
}

function assignedLabel(item) {
  if (isFinalAuthorization(item)) return "Secretaría";
  if (item.assigned_user_name) return item.assigned_user_name;
  if (item.assigned_group_name) {
    return item.claimed_by_name
      ? `${item.assigned_group_name} · ${item.claimed_by_name}`
      : `${item.assigned_group_name} · sin tomar`;
  }
  return "Sin asignar";
}

function isFinalAuthorization(item) {
  return isInspectionWorkflow(item)
    && item.stage_key === "finalizado" && item.status === "completed";
}

function isRejectedAuthorization(item) {
  return isInspectionWorkflow(item)
    && item.stage_key === "rechazado" && item.status === "completed";
}

function isInspectionWorkflow(item) {
  return item.workflow_name === "Autorización Individual" || Boolean(item.is_entity_workflow);
}

export function workflowStatusGroup(item) {
  if (isRejectedAuthorization(item)) return "rejected";
  if (item.delivered_at) return "delivered";
  if (item.status === "completed") return "completed";
  return item.is_observed ? "observed" : "active";
}

function stageAssignmentValue(stage) {
  if (stage.default_assignee_user_id) return `user:${stage.default_assignee_user_id}`;
  if (stage.default_assignee_group_id) return `group:${stage.default_assignee_group_id}`;
  return "";
}

function populateWorkflowSelects() {
  const selects = [byId("workflow-case-definition"), byId("workflow-transition-definition")];
  for (const select of selects) {
    const current = select.value;
    select.replaceChildren();
    for (const workflow of data.workflows) {
      const option = element("option", workflow.name);
      option.value = workflow.id;
      select.append(option);
    }
    if (current && data.workflows.some(workflow => String(workflow.id) === current)) {
      select.value = current;
    }
  }
  populateTransitionStages();
}

function populateTransitionStages() {
  const workflow = data.workflows.find(
    item => String(item.id) === byId("workflow-transition-definition").value,
  );
  for (const select of [byId("workflow-transition-from"), byId("workflow-transition-to")]) {
    const current = select.value;
    select.replaceChildren();
    for (const stage of workflow?.stages || []) {
      const option = element("option", stage.name);
      option.value = stage.id;
      select.append(option);
    }
    if (current && [...select.options].some(option => option.value === current)) {
      select.value = current;
    }
  }
}

function renderUsersAndGroups() {
  const groupMembers = byId("workflow-group-members");
  groupMembers.replaceChildren();
  for (const user of data.users) {
    const option = element("option", user.display_name);
    option.value = user.id;
    groupMembers.append(option);
  }
}

function inspectionMembers() {
  const members = data.groups
    .filter(group => group.name.toLocaleLowerCase("es") === "inspección")
    .flatMap(group => group.members)
    .filter(member => data.users.some(user => user.id === member.id
      && (user.workflow_role === "inspeccion" || user.role === "admin")));
  return [...new Map(members.map(member => [member.id, member])).values()];
}

function updateCaseFields() {
  const workflow = data.workflows.find(
    item => String(item.id) === byId("workflow-case-definition").value,
  );
  const needsInspector = Boolean(workflow?.requires_inspector);
  const isAuthorization = workflow?.name === "Autorización Individual";
  const isEntity = Boolean(workflow?.is_entity_workflow);
  const applicationFields = byId("workflow-application-fields");
  applicationFields.disabled = !isAuthorization;
  applicationFields.classList.toggle("hidden", !isAuthorization);
  const entityFields = byId("workflow-entity-application-fields");
  entityFields.disabled = !isEntity;
  entityFields.classList.toggle("hidden", !isEntity);
  const needsUpdate = Boolean(workflow) && (data.supports_identity_search !== true
    || (isEntity && data.supports_entity_workflows !== true));
  byId("workflow-application-update-warning").classList.toggle("hidden", !needsUpdate);
  byId("workflow-create-submit").disabled = needsUpdate;
  byId("workflow-create-submit").textContent = isEntity ? "Crear y enviar a Inspección"
    : needsInspector ? "Crear y enviar al jefe" : "Crear trámite";
  const requestType = byId("workflow-request-type");
  const linkedSelect = byId("workflow-linked-user");
  const needsLink = needsInspector && ["renovacion", "modificacion"].includes(requestType.value);
  const showLink = !isEntity && (needsLink || (workflow && !needsInspector));
  byId("workflow-request-type-field").classList.toggle("hidden", !needsInspector);
  byId("workflow-linked-user-field").classList.toggle("hidden", !showLink);
  byId("workflow-link-requirement").textContent = needsLink ? "(obligatorio)" : "(opcional)";
  requestType.disabled = !needsInspector;
  requestType.required = needsInspector;
  linkedSelect.disabled = !showLink;
  linkedSelect.required = needsLink && !isEntity;
  const entitySelect = byId("workflow-linked-entity");
  byId("workflow-linked-entity-field").classList.toggle("hidden", !isEntity);
  byId("workflow-entity-link-requirement").textContent = needsLink ? "(obligatorio)" : "(opcional)";
  entitySelect.disabled = !isEntity;
  entitySelect.required = isEntity && needsLink;
  const inspector = byId("workflow-create-inspector");
  byId("workflow-create-inspector-field").classList.toggle("hidden", !isEntity);
  inspector.disabled = !isEntity;
  inspector.required = isEntity;
  if (isEntity) {
    const previous = inspector.value;
    inspector.replaceChildren(element("option", "Selecciona un funcionario de Inspección"));
    inspector.options[0].value = "";
    for (const member of inspectionMembers()) {
      const option = element("option", member.display_name);
      option.value = member.id;
      inspector.append(option);
    }
    inspector.value = previous;
    if (!inspector.value) inspector.selectedIndex = 0;
  }
  if (!showLink) {
    linkedSelect.value = "";
    selectedLinkedUser = "";
    selectedAuthorizationPath = "";
    byId("workflow-linked-user-note").textContent = "";
  }
}

async function fetchLinkedFolders(isEntity, query, offset = 0) {
  const params = isEntity
    ? new URLSearchParams({ q: query, all: "1" })
    : new URLSearchParams({ q: query, offset, limit: linkedUserPageSize, search_identity: "1" });
  const result = await requestJson(`/api/${isEntity ? "entities" : "users"}?${params}`);
  if (isEntity && (result.offset !== 0 || result.items.length !== result.total)) {
    throw new Error("Reinicia la aplicación para cargar la lista completa de entidades.");
  }
  return result;
}

async function loadLinkedEntities() {
  const requestId = ++linkedEntityRequest;
  const select = byId("workflow-linked-entity");
  byId("workflow-linked-entity-note").textContent = "Cargando entidades…";
  try {
    const result = await fetchLinkedFolders(true, byId("workflow-linked-entity-query").value.trim());
    if (requestId !== linkedEntityRequest) return;
    select.replaceChildren(element("option", "Selecciona una entidad"));
    select.options[0].value = "";
    if (selectedLinkedEntity && !result.items.some(item => item.name === selectedLinkedEntity)) {
      const option = element("option", `${selectedLinkedEntity} (seleccionada)`);
      option.value = selectedLinkedEntity;
      select.append(option);
    }
    for (const entity of result.items) {
      const option = element("option", entity.name);
      option.value = entity.name;
      select.append(option);
    }
    select.value = selectedLinkedEntity;
    byId("workflow-linked-entity-note").textContent = `${result.total} entidad(es).` +
      (result.warnings ? ` Hay ${result.warnings} ubicaciones inaccesibles; algunas entidades podrían no aparecer.` : "");
  } catch (error) {
    if (requestId !== linkedEntityRequest) return;
    select.replaceChildren(element("option", "Selecciona una entidad"));
    select.options[0].value = "";
    if (selectedLinkedEntity) {
      const option = element("option", `${selectedLinkedEntity} (seleccionada)`);
      option.value = selectedLinkedEntity;
      select.append(option);
      select.value = selectedLinkedEntity;
    }
    byId("workflow-linked-entity-note").textContent = `No se pudieron cargar las entidades: ${error.message}`;
    setMessage("workflow-message", `No se pudieron buscar las entidades: ${error.message}`, "error");
  }
}

async function loadLinkedUsers(offset = 0) {
  const requestId = ++linkedUserRequest;
  const query = byId("workflow-linked-user-query").value.trim();
  const select = byId("workflow-linked-user");
  try {
    const result = await fetchLinkedFolders(false, query, offset);
    if (requestId !== linkedUserRequest) return;
    linkedUserOffset = result.offset;
    linkedUserTotal = result.total;
    const previousUser = linkedUsers.get(selectedLinkedUser);
    linkedUsers = new Map(result.items.map(user => [user.name, user]));
    if (selectedLinkedUser && previousUser && !linkedUsers.has(selectedLinkedUser)) {
      linkedUsers.set(selectedLinkedUser, previousUser);
    }
    select.replaceChildren(element("option", "Selecciona una carpeta"));
    select.options[0].value = "";
    if (selectedLinkedUser && !result.items.some(user => user.name === selectedLinkedUser)) {
      const option = element("option", `${selectedLinkedUser} (seleccionada)`);
      option.value = selectedLinkedUser;
      select.append(option);
    }
    for (const user of result.items) {
      const option = element("option", user.name);
      option.value = user.name;
      select.append(option);
    }
    select.value = selectedLinkedUser;
    const matched = linkedUsers.get(selectedLinkedUser)?.matched_authorization;
    if (matched && matched.source_path !== selectedAuthorizationPath) {
      selectLinkedUser(true);
    }
    const start = result.total ? result.offset + 1 : 0;
    byId("workflow-linked-user-page").textContent =
      `${start}–${Math.min(result.offset + result.items.length, result.total)} de ${result.total}`;
    byId("workflow-linked-user-prev").disabled = result.offset === 0;
    byId("workflow-linked-user-next").disabled = result.offset + result.items.length >= result.total;
  } catch (error) {
    if (requestId !== linkedUserRequest) return;
    byId("workflow-linked-user-page").textContent = "";
    byId("workflow-linked-user-prev").disabled = true;
    byId("workflow-linked-user-next").disabled = true;
    setMessage("workflow-message", `No se pudieron buscar las carpetas: ${error.message}`, "error");
  }
}

function selectLinkedUser(refresh = false) {
  const select = byId("workflow-linked-user");
  const name = select.value;
  if (name === selectedLinkedUser && !refresh) return;
  const fields = byId("workflow-application-fields");
  if (!fields.disabled) {
    const names = ["first_name", "last_name", "identity_number", "email", "profession"];
    const inputs = names.map(key => fields.querySelector(`[name="${key}"]`));
    const changeDescription = refresh ? "Al cargar la autorización encontrada" : "Al cambiar de carpeta";
    if (inputs.some(input => input.value.trim()) && !window.confirm(
      `${changeDescription} se reemplazarán nombre, apellido y CI, y se limpiarán correo y profesión. ¿Continuar?`,
    )) {
      select.value = selectedLinkedUser;
      if (refresh) {
        byId("workflow-linked-user-note").textContent =
          "Se conservaron los datos anteriores; revisalos antes de crear el trámite.";
      }
      return;
    }
    const user = linkedUsers.get(name);
    const record = user?.matched_authorization || user?.authorization_records?.[0];
    selectedAuthorizationPath = record?.source_path || "";
    for (const input of inputs) input.value = record?.[input.name] || "";
    byId("workflow-linked-user-note").textContent = name
      ? (record
        ? `${user?.matched_authorization ? "Se usó la autorización que coincide con la cédula. " : ""}Revisá los datos: el nombre puede incluir el apellido. Correo y profesión se completan manualmente.`
        : "No hay datos de autorización guardados para esta carpeta. Completá los datos manualmente.")
      : "";
  }
  selectedLinkedUser = name;
}

function normalizeWorkflowSearch(value) {
  return String(value ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("es");
}

export function matchesWorkflowSearch(item, query) {
  const normalizedQuery = normalizeWorkflowSearch(query).trim();
  const terms = normalizedQuery.split(/\s+/).filter(Boolean);
  const identity = String(item.identity_number ?? "").replace(/[.\s-]/g, "");
  const identityMatch = value => {
    if (!/^[\d.\s-]+$/.test(value)) return false;
    const digits = value.replace(/[.\s-]/g, "");
    return digits.length > 0 && identity.includes(digits);
  };
  if (identityMatch(normalizedQuery)) return true;
  const fields = [
    item.id, item.workflow_name, item.request_type, requestTypeLabels[item.request_type], item.title,
    item.applicant_first_name, item.applicant_last_name, item.applicant_company_name,
    item.identity_number, item.form_number, item.short_code, item.assigned_code, item.entity_code,
    item.reference, item.description, item.linked_user_name, item.linked_entity_name,
    item.stage_name, item.assigned_user_name, item.assigned_group_name, item.claimed_by_name, assignedLabel(item),
    item.inspector_name,
  ];
  const text = normalizeWorkflowSearch(fields.filter(value => value != null).join(" "));
  return terms.every(term => text.includes(term) || identityMatch(term));
}

function renderCases() {
  const body = byId("workflow-case-rows");
  const appendCase = item => {
    const row = document.createElement("tr");
    row.className = "workflow-case-row";
    if (item.id === selectedCaseId) row.classList.add("selected");
    row.addEventListener("click", () => selectCase(item.id));
    const type = element("td", item.request_type
      ? `${item.workflow_name} · ${requestTypeLabels[item.request_type] || item.request_type}`
      : item.workflow_name);
    const applicant = [item.applicant_first_name, item.applicant_last_name].filter(Boolean).join(" ");
    const subject = element("td", applicant || item.applicant_company_name || item.title);
    const stage = element("td", item.stage_name);
    const assigned = element("td", assignedLabel(item));
    const status = element("td", isRejectedAuthorization(item) ? "Rechazado"
      : item.delivered_at ? deliveryMethodLabels[item.delivery_method] || "Entregado"
      : item.status === "completed" ? "Finalizado" : item.is_observed ? "Observado" : "En curso");
    const deadlineCell = element("td");
    const deadline = element("span", undefined, "workflow-deadline-badge workflow-deadline");
    deadline.dataset.caseId = item.id;
    updateDeadlineDisplay(deadline, item);
    deadlineCell.append(deadline);
    row.append(type, subject, stage, assigned, status, deadlineCell);
    body.append(row);
  };
  const user = data.current_user;
  const myCases = [];
  const otherCases = [];
  const observedCases = [];
  const rejectedCases = [];
  const pendingDelivery = [];
  const deliveredCases = [];
  const caseGroups = [];
  for (const item of data.cases) {
    if (isRejectedAuthorization(item)) {
      rejectedCases.push(item);
      continue;
    }
    if (item.is_observed) {
      observedCases.push(item);
      continue;
    }
    if ((user.role === "admin" || user.workflow_role === "administracion")
        && isFinalAuthorization(item)) {
      (item.delivered_at ? deliveredCases : pendingDelivery).push(item);
      continue;
    }
    const assignedGroup = data.groups.find(group => group.id === item.assigned_group_id);
    const assignedToMe = item.status === "active" && (
      item.assigned_user_id === user.id
      || (item.claimed_by_user_id === user.id
        && assignedGroup?.members.some(member => member.id === user.id))
    );
    (assignedToMe ? myCases : otherCases).push(item);
  }
  if (user.role === "admin" || user.workflow_role === "jefe_licenciamiento") {
    const groups = [
      { key: "asignacion_inspector", label: "Pendientes de asignar inspector", cases: [] },
      { key: "jefe_licenciamiento", label: "Pendientes de revisión del jefe", cases: [] },
    ];
    const otherMyCases = [];
    for (const item of myCases) {
      const group = isInspectionWorkflow(item)
        && groups.find(candidate => candidate.key === item.stage_key);
      if (group) {
        group.cases.push(item);
      } else {
        otherMyCases.push(item);
      }
    }
    for (const group of groups) {
      caseGroups.push(group);
    }
    if (otherMyCases.length) {
      caseGroups.push({ key: "assigned", label: "Mis trámites asignados", cases: otherMyCases });
    }
  } else {
    caseGroups.push({ key: "assigned", label: "Mis trámites asignados", cases: myCases });
  }
  caseGroups.push({ key: "observed", label: "Trámites observados", cases: observedCases });
  caseGroups.push({ key: "rejected", label: "Trámites rechazados", cases: rejectedCases });
  if (user.role === "admin" || user.workflow_role === "administracion") {
    caseGroups.push({ key: "pending-delivery", label: "Finalizados pendientes de entrega", cases: pendingDelivery });
    caseGroups.push({ key: "delivered", label: "Entregados", cases: deliveredCases });
  }
  caseGroups.push({ key: "other", label: "Trámites generales", cases: otherCases });

  const query = byId("workflow-case-search").value.trim();
  if (query) {
    caseGroups.unshift({
      key: "search", label: "Resultados de búsqueda",
      cases: data.cases.filter(item => matchesWorkflowSearch(item, query)),
    });
  }
  const selectedGroup = caseGroups.find(group =>
    (group.key !== "search" || selectedCaseGroup === "search")
      && group.cases.some(item => item.id === selectedCaseId));
  if (selectedGroup) {
    selectedCaseGroup = selectedGroup.key;
  } else if (!caseGroups.some(group => group.key === selectedCaseGroup)) {
    selectedCaseGroup = (caseGroups.find(group => group.cases.length) || caseGroups[0]).key;
  }
  const tabs = byId("workflow-case-tabs");
  for (const button of [...tabs.children]) {
    if (!caseGroups.some(group => group.key === button.dataset.group)) button.remove();
  }
  caseGroups.forEach((group, index) => {
    let button = [...tabs.children].find(child => child.dataset.group === group.key);
    if (!button) {
      button = element("button", undefined, "workflow-case-tab");
      button.type = "button";
      button.dataset.group = group.key;
      button.addEventListener("click", () => {
        if (selectedCaseGroup === group.key) return;
        selectedCaseGroup = group.key;
        selectedCaseId = null;
        workflowRevision += 1;
        renderCases();
        renderTransitions();
      });
    }
    if (tabs.children[index] !== button) tabs.insertBefore(button, tabs.children[index] || null);
    button.textContent = `${group.label} (${group.cases.length})`;
    button.setAttribute("aria-pressed", String(group.key === selectedCaseGroup));
  });

  body.replaceChildren();
  const activeCases = caseGroups.find(group => group.key === selectedCaseGroup).cases;
  if (activeCases.length) {
    activeCases.forEach(appendCase);
  } else {
    const row = document.createElement("tr");
    const empty = element("td", selectedCaseGroup === "search"
      ? "No se encontraron trámites para esta búsqueda." : "No hay trámites en este grupo.", "workflow-case-empty");
    empty.colSpan = 6;
    row.append(empty);
    body.append(row);
  }
}

function canActOn(item) {
  const user = data.current_user;
  if (user.role === "admin") return true;
  if (item.required_role && item.required_role !== user.workflow_role) return false;
  if (item.assigned_user_id === user.id) return true;
  if (!item.assigned_group_id) return false;
  const group = data.groups.find(candidate => candidate.id === item.assigned_group_id);
  const isMember = group?.members.some(member => member.id === user.id);
  return Boolean(isMember && item.claimed_by_user_id === user.id);
}

function addAssignmentOptions(select, selectedValue = "", requiredRole = "") {
  select.append(element("option", "Sin responsable"));
  select.options[0].value = "";
  for (const user of data.users) {
    if (requiredRole && user.role !== "admin" && user.workflow_role !== requiredRole) continue;
    const option = element("option", user.display_name);
    option.value = `user:${user.id}`;
    select.append(option);
  }
  for (const group of data.groups) {
    if (requiredRole && !group.members.some(member => data.users.some(
      user => user.id === member.id && user.workflow_role === requiredRole,
    ))) continue;
    const option = element("option", `Grupo: ${group.name}`);
    option.value = `group:${group.id}`;
    select.append(option);
  }
  select.value = selectedValue;
}

function renderAssignmentSettings() {
  const container = byId("workflow-stage-assignments");
  container.replaceChildren(element("h3", "Responsables predeterminados por etapa"));
  for (const workflow of data.workflows) {
    const section = element("section", undefined, "workflow-assignment-section");
    section.append(element("h4", workflow.name));
    for (const stage of workflow.stages) {
      const row = element("div", undefined, "workflow-assignment-row");
      row.append(element("span", stage.name));
      const select = document.createElement("select");
      select.setAttribute("aria-label", `Responsable predeterminado: ${stage.name}`);
      addAssignmentOptions(select, stageAssignmentValue(stage), stage.required_role);
      select.addEventListener("change", async () => {
        const [kind, id] = select.value ? select.value.split(":") : ["", ""];
        try {
          await requestJson("/api/workflow/stage-assignment", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              stage_id: stage.id,
              user_id: kind === "user" ? Number(id) : null,
              group_id: kind === "group" ? Number(id) : null,
            }),
          });
          await refreshWorkflows();
          setMessage("workflow-message", "Responsable predeterminado actualizado.", "success");
        } catch (error) {
          setMessage("workflow-message", error.message, "error");
          select.value = stageAssignmentValue(stage);
        }
      });
      row.append(select);
      section.append(row);
    }
    container.append(section);
  }
}

function renderInspectorFolderLink(selected) {
  const isEntity = Boolean(selected.is_entity_workflow);
  const panel = document.createElement("details");
  panel.className = "workflow-inspector-link";
  panel.append(element("summary", isEntity ? "Vincular carpeta de entidad" : "Vincular carpeta de usuario"));
  const form = element("form", undefined, "workflow-inline-form");
  const searchLabel = element("label", isEntity ? "Buscar entidad" : "Buscar carpeta de usuario");
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = isEntity ? "Nombre de la entidad" : "Nombre, apellido o cédula";
  searchLabel.append(search);
  const searchButton = element("button", "Buscar", "button secondary small");
  searchButton.type = "button";
  const selectLabel = element("label", isEntity ? "Entidad" : "Carpeta");
  const select = document.createElement("select");
  select.required = true;
  select.disabled = true;
  selectLabel.append(select);
  const previous = element("button", "Anterior", "button secondary small");
  previous.type = "button";
  previous.disabled = true;
  const next = element("button", "Siguiente", "button secondary small");
  next.type = "button";
  next.disabled = true;
  const status = element("span", "", "form-help");
  status.setAttribute("role", "status");
  const save = element("button", "Vincular carpeta", "button primary");
  save.type = "submit";
  save.disabled = true;
  form.append(searchLabel, searchButton, selectLabel);
  if (!isEntity) form.append(previous, next);
  form.append(status, save);
  panel.append(form);
  let requestId = 0;
  let offset = 0;
  let total = 0;
  let saving = false;

  async function loadFolders(nextOffset = 0) {
    const request = ++requestId;
    select.disabled = true;
    save.disabled = true;
    previous.disabled = true;
    next.disabled = true;
    status.textContent = "Cargando carpetas…";
    try {
      const result = await fetchLinkedFolders(isEntity, search.value.trim(), nextOffset);
      if (request !== requestId || !panel.isConnected) return;
      offset = result.offset;
      total = result.total;
      const placeholder = element("option", isEntity ? "Selecciona una entidad" : "Selecciona una carpeta");
      placeholder.value = "";
      select.replaceChildren(placeholder);
      for (const item of result.items) {
        const option = element("option", item.name);
        option.value = item.name;
        select.append(option);
      }
      select.value = "";
      select.disabled = false;
      previous.disabled = offset === 0;
      next.disabled = offset + result.items.length >= total;
      const first = total ? offset + 1 : 0;
      status.textContent = `${first}–${Math.min(offset + result.items.length, total)} de ${total}.` +
        (result.warnings ? ` Hay ${result.warnings} ubicaciones inaccesibles; pueden faltar carpetas.` : "");
    } catch (error) {
      if (request !== requestId || !panel.isConnected) return;
      status.textContent = `No se pudieron cargar las carpetas: ${error.message}`;
    }
  }

  panel.addEventListener("toggle", () => {
    if (panel.open && !requestId) loadFolders();
  });
  searchButton.addEventListener("click", () => loadFolders());
  search.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadFolders();
    }
  });
  previous.addEventListener("click", () => loadFolders(Math.max(0, offset - linkedUserPageSize)));
  next.addEventListener("click", () => {
    if (offset + linkedUserPageSize < total) loadFolders(offset + linkedUserPageSize);
  });
  select.addEventListener("change", () => { save.disabled = !select.value || saving; });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!select.value || saving) return;
    saving = true;
    search.disabled = true;
    searchButton.disabled = true;
    select.disabled = true;
    previous.disabled = true;
    next.disabled = true;
    save.disabled = true;
    ++workflowRevision;
    try {
      await requestJson("/api/workflow/cases/link-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_id: selected.id, kind: isEntity ? "entity" : "user", name: select.value }),
      });
    } catch (error) {
      setMessage("workflow-message",
        `No se pudo confirmar la vinculación: ${error.message}. Actualiza antes de volver a intentar.`,
        "error");
      return;
    }
    try {
      await loadWorkflowData();
      setMessage("workflow-message", "Carpeta vinculada al trámite.", "success");
    } catch (error) {
      setMessage("workflow-message",
        `La carpeta se vinculó, pero no se pudo actualizar la vista: ${error.message}. Pulsa Actualizar.`,
        "error");
    }
  });
  return panel;
}

function renderTransitions() {
  const container = byId("workflow-case-detail");
  const selected = data.cases.find(item => item.id === selectedCaseId);
  if (!selected) {
    container.className = "workflow-detail empty-state";
    container.textContent = "Selecciona un trámite para ver su información e historial.";
    return;
  }
  container.className = "workflow-detail";
  const detail = data.detail;
  const summary = element("div", undefined, "workflow-case-summary");
  const applicant = [selected.applicant_first_name, selected.applicant_last_name].filter(Boolean).join(" ");
  summary.append(element("h3", applicant || selected.applicant_company_name || selected.title));
  summary.append(element("p", `${selected.workflow_name} · ${selected.stage_name}`));
  if (selected.is_observed) summary.append(element("p", "Estado: Observado"));
  if (isRejectedAuthorization(selected)) summary.append(element("p", "Estado: Rechazado"));
  if (selected.request_type) summary.append(element("p", `Solicitud: ${requestTypeLabels[selected.request_type] || selected.request_type}`));
  if (selected.inspector_name) summary.append(element("p", `Inspector asignado: ${selected.inspector_name}`));
  if (selected.reference) summary.append(element("p", `Referencia: ${selected.reference}`));
  for (const [label, name, kind] of [
    ["Carpeta vinculada", selected.linked_user_name, "users"],
    ["Entidad vinculada", selected.linked_entity_name, "entities"],
  ]) {
    if (!name) continue;
    const line = element("p", `${label}: `);
    const link = element("a", name, "workflow-profile-link");
    link.href = `#pane-${kind}`;
    link.addEventListener("click", event => {
      event.preventDefault();
      window.dispatchEvent(new CustomEvent("workflow-open-linked-profile", { detail: { kind, name } }));
    });
    line.append(link);
    summary.append(line);
  }
  if (selected.description) summary.append(element("p", selected.description));
  summary.append(element("p", `Responsable: ${assignedLabel(selected)}`));
  summary.append(element("p", `Ingresó a la etapa: ${formatDate(selected.entered_stage_at)}`));
  const deadlineLine = element("p");
  const deadline = element("span", undefined, "workflow-deadline-badge workflow-deadline-detail");
  updateDeadlineDisplay(deadline, selected, true);
  deadlineLine.append(deadline);
  summary.append(deadlineLine);
  if (selected.status !== "active") {
    summary.append(element("p", `Finalizado: ${formatDate(selected.completed_at)}`));
    if (selected.delivered_at) {
      summary.append(element("p", `Entregado: ${formatDate(selected.delivered_at)} · ${selected.delivered_by_name}`));
      if (selected.delivery_method) {
        summary.append(element("p", deliveryMethodLabels[selected.delivery_method]));
      }
    }
  }
  container.replaceChildren(summary);
  const applicationData = detail?.application_data || detail?.entity_application_data;
  if (applicationData) {
    const section = element("section", undefined, "workflow-application-data");
    section.append(element("h4", "Datos de la solicitud"));
    const list = document.createElement("dl");
    const labels = detail?.entity_application_data ? entityApplicationFieldLabels : applicationFieldLabels;
    for (const [key, label] of Object.entries(labels)) {
      const item = document.createElement("div");
      item.append(element("dt", label));
      const value = applicationData[key];
      item.append(element("dd", key === "digital_signature" ? (value ? "Sí" : "No") : value));
      list.append(item);
    }
    section.append(list);
    container.append(section);
  }

  if (isFinalAuthorization(selected) && !selected.delivered_at
      && (data.current_user.role === "admin" || data.current_user.workflow_role === "administracion")) {
    const deliveryForm = element("div", undefined, "workflow-inline-form");
    const deliveryLabel = element("label", "Modalidad de entrega");
    const deliverySelect = document.createElement("select");
    const placeholder = element("option", "Selecciona una modalidad");
    placeholder.value = "";
    deliverySelect.append(placeholder);
    for (const [value, label] of Object.entries(deliveryMethodLabels)) {
      const option = element("option", label);
      option.value = value;
      deliverySelect.append(option);
    }
    deliveryLabel.append(deliverySelect);
    const deliverButton = makeButton("Marcar como entregado", "deliver-workflow-case", "button primary");
    deliverButton.disabled = true;
    deliverySelect.addEventListener("change", () => {
      deliverButton.disabled = !deliverySelect.value;
    });
    deliverButton.addEventListener("click", async () => {
      if (!deliverySelect.value || deliverButton.disabled) return;
      deliverButton.disabled = true;
      deliverySelect.disabled = true;
      ++workflowRevision;
      try {
        await requestJson("/api/workflow/cases/deliver", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ case_id: selected.id, delivery_method: deliverySelect.value }),
        });
      } catch (error) {
        setMessage("workflow-message",
          `No se pudo confirmar la entrega: ${error.message}. Actualiza antes de volver a intentar.`,
          "error");
        return;
      }
      try {
        await loadWorkflowData();
        setMessage("workflow-message", "Trámite marcado como entregado.", "success");
      } catch (error) {
        setMessage("workflow-message",
          `La entrega se registró, pero no se pudo actualizar la vista: ${error.message}. Pulsa Actualizar.`,
          "error");
      }
    });
    deliveryForm.append(deliveryLabel, deliverButton);
    container.append(deliveryForm);
  }

  if (selected.status === "active" && selected.assigned_group_id
      && !selected.claimed_by_user_id) {
    const group = data.groups.find(item => item.id === selected.assigned_group_id);
    if ((!selected.required_role || selected.required_role === data.current_user.workflow_role)
        && group?.members.some(member => member.id === data.current_user.id)) {
      const claimButton = makeButton("Tomar trámite", "claim-workflow-case", "button primary");
      claimButton.addEventListener("click", () => performAction("/api/workflow/cases/claim", {
        case_id: selected.id,
      }));
      container.append(claimButton);
    }
  }

  if (selected.status === "active" && canActOn(selected)) {
    if (isInspectionWorkflow(selected) && selected.stage_key === "evaluacion_inspector"
        && !(selected.is_entity_workflow ? selected.linked_entity_name : selected.linked_user_name)) {
      if (data.supports_inspector_folder_link === true) {
        container.append(renderInspectorFolderLink(selected));
      } else {
        container.append(element("p", "Reinicia la aplicación para habilitar la vinculación de carpetas.", "form-help"));
      }
    }
    const canAssign = data.current_user.role === "admin"
      || data.current_user.workflow_role === "administracion";
    if (canAssign && !selected.is_observed) {
      const assignmentForm = document.createElement("form");
      assignmentForm.className = "workflow-inline-form";
      assignmentForm.append(element("strong", "Reasignar a"));
      const assignmentSelect = document.createElement("select");
      const currentAssignment = selected.assigned_user_id
        ? `user:${selected.assigned_user_id}`
        : selected.assigned_group_id ? `group:${selected.assigned_group_id}` : "";
      addAssignmentOptions(assignmentSelect, currentAssignment, selected.required_role);
      assignmentForm.append(assignmentSelect);
      const assignButton = element("button", "Asignar", "button secondary small");
      assignButton.type = "submit";
      assignmentForm.append(assignButton);
      assignmentForm.addEventListener("submit", event => {
        event.preventDefault();
        const [kind, id] = assignmentSelect.value ? assignmentSelect.value.split(":") : ["", ""];
        if (!id) {
          setMessage("workflow-message", "Selecciona una persona o grupo para asignar.", "error");
          return;
        }
        performAction("/api/workflow/cases/assignment", {
          case_id: selected.id,
          user_id: kind === "user" ? Number(id) : null,
          group_id: kind === "group" ? Number(id) : null,
        });
      });
      container.append(assignmentForm);
    }

    if (detail?.transitions.length) {
      container.append(element("h4", "Acciones disponibles"));
      const comment = document.createElement("textarea");
      comment.placeholder = "Comentario o motivo (opcional)";
      comment.maxLength = 1000;
      comment.setAttribute("aria-label", "Comentario de la acción");
      container.append(comment);
      let assignmentTransition = null;
      const observedReturn = selected.is_observed
        && data.supports_observed_inspector_reassignment === true;
      if (isInspectionWorkflow(selected)
          && (selected.stage_key === "asignacion_inspector" || observedReturn)) {
        assignmentTransition = detail.transitions.find(
          transition => transition.to_stage_key === "evaluacion_inspector",
        );
      }
      if (assignmentTransition) {
        const label = element("label", observedReturn ? "Reasignar a:" : "Funcionario de Inspección");
        label.className = "workflow-inspector-select";
        const inspectorSelect = document.createElement("select");
        const members = inspectionMembers();
        const placeholder = element("option", members.length
          ? "Selecciona un funcionario" : "No hay integrantes en Inspección");
        placeholder.value = "";
        inspectorSelect.append(placeholder);
        for (const member of members) {
          const option = element("option", member.display_name);
          option.value = member.id;
          inspectorSelect.append(option);
        }
        if (observedReturn && selected.inspector_id) inspectorSelect.value = String(selected.inspector_id);
        const sendButton = makeButton(observedReturn ? "Devolver a Inspección" : "Enviar a Inspección",
          "send-to-inspection", "button primary");
        let sending = false;
        const updateSendButton = () => {
          sendButton.disabled = sending || !inspectorSelect.value;
        };
        inspectorSelect.addEventListener("change", updateSendButton);
        updateSendButton();
        sendButton.addEventListener("click", async () => {
          if (sending || !inspectorSelect.value) return;
          sending = true;
          inspectorSelect.disabled = true;
          sendButton.disabled = true;
          ++workflowRevision;
          try {
            await requestJson("/api/workflow/cases/transition", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                case_id: selected.id,
                transition_id: assignmentTransition.id,
                inspector_id: Number(inspectorSelect.value),
                comment: comment.value,
              }),
            });
          } catch (error) {
            setMessage("workflow-message",
              `No se pudo confirmar la asignación: ${error.message}. Actualiza antes de volver a intentar.`,
              "error");
            return;
          }
          try {
            await loadWorkflowData();
            setMessage("workflow-message", "Funcionario asignado y trámite enviado a Inspección.", "success");
          } catch (error) {
            setMessage("workflow-message",
              `El trámite se envió, pero no se pudo actualizar la vista: ${error.message}. Pulsa Actualizar.`,
              "error");
          }
        });
        label.append(inspectorSelect);
        container.append(label);
        container.append(element("p", "Selecciona un funcionario para habilitar el envío. El comentario es opcional.", "form-help"));
        container.append(sendButton);
      }
      const inspectionActions = isInspectionWorkflow(selected)
        && selected.stage_key === "evaluacion_inspector";
      const directorActions = isInspectionWorkflow(selected)
        && selected.stage_key === "director";
      const reviewActions = isInspectionWorkflow(selected)
        && ["jefe_licenciamiento", "abogado", "director"].includes(selected.stage_key);
      const actions = element("div", undefined, inspectionActions
        ? "workflow-actions workflow-inspection-actions"
        : directorActions ? "workflow-actions workflow-review-actions workflow-director-actions"
          : reviewActions ? "workflow-actions workflow-review-actions" : "workflow-actions");
      if (inspectionActions && data.supports_inspection_rejection !== true) {
        container.append(element("p",
          "Actualiza el servidor para habilitar el rechazo de trámites.", "form-help"));
      }
      if (selected.is_observed && data.supports_observed_inspector_reassignment !== true
          && detail.transitions.some(transition => transition.to_stage_key === "evaluacion_inspector")) {
        container.append(element("p",
          "Actualiza el servidor para seleccionar otro funcionario de Inspección.", "form-help"));
      }
      const returnTarget = selected.stage_key === "abogado" ? "jefe_licenciamiento" : "evaluacion_inspector";
      const forwardTarget = selected.stage_key === "abogado" ? "director" : "abogado";
      const actionOrder = inspectionActions
        ? { rechazado: 0, carga_datos: 1, jefe_licenciamiento: 2 }
        : directorActions ? { evaluacion_inspector: 0, jefe_licenciamiento: 1, finalizado: 2 }
          : { [returnTarget]: 0, [forwardTarget]: 1 };
      const transitions = inspectionActions || reviewActions
        ? [...detail.transitions].sort((a, b) =>
          (actionOrder[a.to_stage_key] ?? 3) - (actionOrder[b.to_stage_key] ?? 3))
        : detail.transitions;
      for (const transition of transitions) {
        if (transition.id === assignmentTransition?.id) continue;
        const label = inspectionActions && transition.to_stage_key === "carga_datos"
          ? "Observado: devolver a Secretaría"
          : selected.is_observed && transition.to_stage_key === "evaluacion_inspector"
            ? "Devolver a Inspección" : transition.label;
        const actionStyle = inspectionActions
          ? { rechazado: "workflow-reject", carga_datos: "workflow-observe",
            jefe_licenciamiento: "workflow-approve" }[transition.to_stage_key]
          : directorActions
            ? { evaluacion_inspector: "workflow-observe", jefe_licenciamiento: "workflow-observe",
              finalizado: "workflow-approve" }[transition.to_stage_key]
          : reviewActions
            ? { [returnTarget]: "workflow-observe", [forwardTarget]: "workflow-approve" }[transition.to_stage_key]
            : null;
        const button = makeButton(label, "transition-workflow-case",
          `button ${actionStyle || "primary"}`);
        button.addEventListener("click", () => {
          if (inspectionActions && transition.to_stage_key === "rechazado") {
            if (!comment.value.trim()) {
              setMessage("workflow-message", "Ingresa el motivo del rechazo.", "error");
              comment.focus();
              return;
            }
            if (!window.confirm("El trámite quedará rechazado y finalizado. ¿Confirmas el rechazo?")) return;
          }
          const body = { case_id: selected.id, transition_id: transition.id, comment: comment.value };
          performAction("/api/workflow/cases/transition", body);
        });
        actions.append(button);
      }
      container.append(actions);
    }
  }

  const history = element("section", undefined, "workflow-history");
  history.append(element("h4", "Historial"));
  if (!detail?.events.length) {
    history.append(element("p", "No hay eventos registrados."));
  } else {
    const list = document.createElement("ol");
    for (const event of detail.events) {
      const item = document.createElement("li");
      const descriptions = {
        created: "inició el trámite",
        assigned: "cambió la asignación",
        claimed: "tomó el trámite del grupo",
        delivered: "marcó el trámite como entregado",
        folder_linked: "vinculó una carpeta",
      };
      item.append(element("strong", event.actor_name));
      if (event.event_type === "transitioned") {
        item.append(" avanzó de ", element("strong", event.from_stage_name), " a ",
                    element("strong", event.to_stage_name));
      } else {
        item.append(` ${descriptions[event.event_type] || event.event_type}`);
      }
      item.append(` · ${formatDate(event.created_at)}`);
      if (event.event_type === "assigned") {
        const assignee = event.to_user_name || event.to_group_name || "Sin asignar";
        item.append(" · Responsable: ", element("strong", assignee));
      }
      if (event.event_type === "transitioned" && (event.to_user_name || event.to_group_name)) {
        item.append(" · Responsable: ", element("strong", event.to_user_name || event.to_group_name));
      } else if (event.event_type === "transitioned" && event.to_stage_key === "finalizado"
          && isInspectionWorkflow(selected)) {
        item.append(" · Responsable: ", element("strong", "Secretaría"));
      }
      if (event.event_type === "delivered" && event.duration_seconds !== null
          && event.duration_seconds !== undefined) {
        item.append(element("span", ` · Tiempo hasta la entrega: ${formatDuration(event.duration_seconds)}`));
      }
      if (event.comment) item.append(element("p", event.comment));
      list.append(item);
    }
    history.append(list);
  }
  container.append(history);
}

function renderAdmin() {
  const panel = byId("workflow-admin-panel");
  const isAdmin = data.current_user.role === "admin";
  panel.classList.toggle("hidden", !isAdmin);
  if (!isAdmin) return;
  renderAssignmentSettings();
}

function render() {
  byId("workflow-export").classList.toggle("hidden", data.supports_workflow_export !== true);
  byId("workflow-case-search").disabled = data.supports_workflow_case_search !== true;
  byId("workflow-case-search-note").classList.toggle("hidden", data.supports_workflow_case_search === true);
  byId("workflow-observed-update-warning").classList.toggle("hidden", data.supports_observed_status === true);
  byId("workflow-current-user").textContent =
    `${data.current_user.display_name} (${data.current_user.role === "admin" ? "Administrador" : roleLabels[data.current_user.workflow_role] || "Usuario sin rol"})`;
  document.querySelector("#pane-workflows .workflow-create-panel").classList.toggle(
    "hidden", data.current_user.role !== "admin" && data.current_user.workflow_role !== "administracion",
  );
  renderUsersAndGroups();
  populateWorkflowSelects();
  updateCaseFields();
  renderCases();
  renderTransitions();
  renderAdmin();
}

async function loadWorkflowData() {
  const revision = ++workflowRevision;
  const query = selectedCaseId ? `?case_id=${encodeURIComponent(selectedCaseId)}` : "";
  const result = await requestJson(`/api/workflow${query}`);
  if (revision !== workflowRevision) return;
  data = result;
  render();
}

async function refreshCaseList() {
  if (refreshingCaseList) return;
  refreshingCaseList = true;
  const revision = workflowRevision;
  try {
    const result = await requestJson("/api/workflow");
    if (revision !== workflowRevision) return;
    const previous = data.cases.find(item => item.id === selectedCaseId);
    const current = result.cases.find(item => item.id === selectedCaseId);
    data.cases = result.cases;
    data.groups = result.groups;
    data.users = result.users;
    if (previous && (!current || ["current_stage_id", "assigned_user_id", "assigned_group_id", "is_observed",
      "claimed_by_user_id", "status", "updated_at", "delivered_at", "linked_user_name",
      "linked_entity_name", "deadline_started_at", "deadline_paused_at"].some(key => previous[key] !== current[key]))) {
      await loadWorkflowData();
    } else {
      renderCases();
    }
  } catch (error) {
    setMessage("workflow-message", `No se pudo actualizar la lista de trámites: ${error.message}`, "error");
  } finally {
    refreshingCaseList = false;
  }
}

async function refreshWorkflows() {
  await loadWorkflowData();
}

async function performAction(url, body) {
  ++workflowRevision;
  try {
    await requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadWorkflowData();
    setMessage("workflow-message", "Cambios guardados.", "success");
  } catch (error) {
    setMessage("workflow-message", error.message, "error");
  }
}

async function selectCase(id) {
  selectedCaseId = id;
  try {
    await loadWorkflowData();
  } catch (error) {
    setMessage("workflow-message", error.message, "error");
  }
}

function addJsonSubmit(formId, url, makeBody, onSuccess) {
  byId(formId).addEventListener("submit", async event => {
    event.preventDefault();
    try {
      await requestJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(makeBody()),
      });
      byId(formId).reset();
      await loadWorkflowData();
      if (onSuccess) onSuccess();
      setMessage("workflow-message", "Cambios guardados.", "success");
    } catch (error) {
      setMessage("workflow-message", error.message, "error");
    }
  });
}

export async function loadWorkflows() {
  try {
    await loadWorkflowData();
    await loadLinkedUsers();
    if (!byId("workflow-linked-entity").disabled) await loadLinkedEntities();
    return true;
  } catch (error) {
    setMessage("workflow-message", error.message, "error");
    return false;
  }
}

export function initWorkflows() {
  byId("workflow-case-definition").addEventListener("change", () => {
    byId("workflow-request-type").value = "";
    selectedLinkedEntity = "";
    byId("workflow-linked-entity").value = "";
    byId("workflow-linked-entity-query").value = "";
    byId("workflow-create-inspector").value = "";
    linkedEntityRequest += 1;
    updateCaseFields();
    if (!byId("workflow-linked-entity").disabled) loadLinkedEntities();
  });
  byId("workflow-request-type").addEventListener("change", updateCaseFields);
  byId("workflow-linked-user-search").addEventListener("click", () => loadLinkedUsers());
  byId("workflow-linked-user").addEventListener("change", () => selectLinkedUser());
  byId("workflow-linked-user-query").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadLinkedUsers();
    }
  });
  byId("workflow-linked-user-prev").addEventListener("click", () => {
    loadLinkedUsers(Math.max(0, linkedUserOffset - linkedUserPageSize));
  });
  byId("workflow-linked-user-next").addEventListener("click", () => {
    if (linkedUserOffset + linkedUserPageSize < linkedUserTotal) {
      loadLinkedUsers(linkedUserOffset + linkedUserPageSize);
    }
  });
  byId("workflow-linked-entity-search").addEventListener("click", () => loadLinkedEntities());
  byId("workflow-linked-entity").addEventListener("change", () => {
    selectedLinkedEntity = byId("workflow-linked-entity").value;
  });
  byId("workflow-linked-entity-query").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadLinkedEntities();
    }
  });
  byId("workflow-case-search").addEventListener("input", () => {
    selectedCaseId = null;
    selectedCaseGroup = byId("workflow-case-search").value.trim() ? "search" : null;
    workflowRevision += 1;
    renderCases();
    renderTransitions();
  });
  byId("workflow-refresh").addEventListener("click", async () => {
    try {
      await loadWorkflowData();
      setMessage("workflow-message");
    } catch (error) {
      setMessage("workflow-message", error.message, "error");
    }
  });
  byId("workflow-case-form").addEventListener("submit", async event => {
    event.preventDefault();
    const workflowId = Number(byId("workflow-case-definition").value);
    if (!workflowId) return;
    if (data.supports_identity_search !== true) {
      setMessage("workflow-message", "Reinicia la aplicación para iniciar trámites con el formulario actualizado.", "error");
      return;
    }
    const applicationFields = byId("workflow-application-fields").disabled
      ? byId("workflow-entity-application-fields") : byId("workflow-application-fields");
    let applicationData = null;
    if (!applicationFields.disabled) {
      applicationData = Object.fromEntries(
        [...applicationFields.querySelectorAll("[name]")].map(input => [input.name, input.value]),
      );
      if (!["yes", "no"].includes(applicationData.digital_signature)) {
        setMessage("workflow-message", "Indica si tiene firma digital.", "error");
        return;
      }
      applicationData.digital_signature = applicationData.digital_signature === "yes";
    }
    try {
      const result = await requestJson("/api/workflow/cases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workflow_id: workflowId,
          description: byId("workflow-case-description").value.trim(),
          request_type: byId("workflow-request-type").disabled ? "" : byId("workflow-request-type").value,
          linked_user_name: byId("workflow-linked-user").disabled ? "" : byId("workflow-linked-user").value,
          linked_entity_name: byId("workflow-linked-entity").disabled ? "" : byId("workflow-linked-entity").value,
          inspector_id: byId("workflow-create-inspector").disabled
            ? null : Number(byId("workflow-create-inspector").value),
          application_data: applicationData,
        }),
      });
      selectedCaseId = result.id;
      event.target.reset();
      selectedLinkedUser = "";
      selectedLinkedEntity = "";
      selectedAuthorizationPath = "";
      byId("workflow-linked-user-note").textContent = "";
      updateCaseFields();
      await loadWorkflowData();
      setMessage("workflow-message", result.sent_to_inspection ? "Trámite creado y enviado a Inspección."
        : result.sent_to_chief ? "Trámite creado y enviado al jefe." : "Trámite creado.", "success");
    } catch (error) {
      setMessage("workflow-message", error.message, "error");
    }
  });

  addJsonSubmit("workflow-user-form", "/api/workflow/users", () => ({
    display_name: byId("workflow-user-name").value.trim(),
    username: byId("workflow-username").value.trim(),
    password: byId("workflow-user-password").value,
    role: byId("workflow-user-role").value,
  }));
  addJsonSubmit("workflow-group-form", "/api/workflow/groups", () => ({
    name: byId("workflow-group-name").value.trim(),
    member_ids: [...byId("workflow-group-members").selectedOptions].map(option => Number(option.value)),
  }));
  addJsonSubmit("workflow-definition-form", "/api/workflow/definitions", () => ({
    name: byId("workflow-definition-name").value.trim(),
    description: byId("workflow-definition-description").value.trim(),
    stages: byId("workflow-definition-stages").value.split(/\r?\n/).map(stage => stage.trim()).filter(Boolean),
  }));
  byId("workflow-transition-definition").addEventListener("change", populateTransitionStages);
  addJsonSubmit("workflow-transition-form", "/api/workflow/transitions", () => ({
    workflow_id: Number(byId("workflow-transition-definition").value),
    from_stage_id: Number(byId("workflow-transition-from").value),
    to_stage_id: Number(byId("workflow-transition-to").value),
    label: byId("workflow-transition-label").value.trim(),
  }));

  byId("workflow-logout").addEventListener("click", async () => {
    try {
      setMessage("session-message");
      await requestJson("/api/logout", { method: "POST" });
      window.location.assign("/");
    } catch (error) {
      setMessage("session-message", error.message, "error");
    }
  });

  window.setInterval(() => {
    if (!data || document.querySelector(".tab.active")?.dataset.tab !== "workflows") return;
    const casesById = new Map(data.cases.map(item => [String(item.id), item]));
    for (const target of document.querySelectorAll(".workflow-deadline[data-case-id]")) {
      const item = casesById.get(target.dataset.caseId);
      if (item && item.status === "active") updateDeadlineDisplay(target, item);
    }
    const selected = data.cases.find(item => item.id === selectedCaseId);
    const detail = document.querySelector(".workflow-deadline-detail");
    if (selected?.status === "active" && detail) updateDeadlineDisplay(detail, selected, true);
  }, 1000);
  window.setInterval(() => {
    if (data && document.querySelector('.tab.active')?.dataset.tab === "workflows") {
      refreshCaseList();
    }
  }, 30_000);
}
