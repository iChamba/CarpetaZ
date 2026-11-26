import { lastUpdateText, requestJson, setMessage } from "./common.js";
import { initRecords, loadRecords, loadWorkflowRecords } from "./records.js";
import { initUsers, invalidateLoadedUsers, loadUsers, openLinkedUser } from "./users.js";
import { initEntities, loadEntities, openLinkedEntity } from "./entities.js";
import { initWorkflows, loadWorkflows } from "./workflows.js";
import { initInspections, loadInspections } from "./inspections.js";

const tabs = [...document.querySelectorAll(".tab")];
const panes = [...document.querySelectorAll(".pane")];
const loadedTabs = new Set();
const tabLoadTokens = new Map();

window.addEventListener("records-stale", () => loadedTabs.delete("records"));
window.addEventListener("users-stale", () => {
  loadedTabs.delete("users");
  invalidateLoadedUsers();
});

function activateTab(name, load = true) {
  tabs.forEach(tab => tab.classList.toggle("active", tab.dataset.tab === name));
  panes.forEach(pane => pane.classList.toggle("active", pane.id === `pane-${name}`));
  if (!load) return;
  if (name === "users") loadTabOnce(name, loadUsers);
  if (name === "workflows") loadTabOnce(name, loadWorkflows);
  if (name === "inspections") loadInspections();
  if (name === "entities") loadTabOnce(name, loadEntities);
  if (name === "records") {
    if (loadedTabs.has(name)) loadWorkflowRecords();
    else loadTabOnce(name, loadRecords);
  }
}

function loadTabOnce(name, loader) {
  if (loadedTabs.has(name)) return;
  loadedTabs.add(name);
  const token = Symbol(name);
  tabLoadTokens.set(name, token);
  loader().then(loaded => {
    if (loaded === false && tabLoadTokens.get(name) === token) loadedTabs.delete(name);
  });
}

tabs.forEach(tab => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
document.getElementById("settings-open").addEventListener("click", () => activateTab("settings"));
document.getElementById("password-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  if (values.new_password !== values.confirm_password) {
    setMessage("password-message", "La nueva contraseña y su confirmación no coinciden.", "error");
    return;
  }
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await requestJson("/api/account/password", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
    });
    form.reset();
    setMessage("password-message", "Contraseña actualizada. Las otras sesiones abiertas se cerraron.", "success");
  } catch (error) {
    setMessage("password-message", error.message, "error");
  } finally {
    button.disabled = false;
  }
});
window.addEventListener("workflow-open-linked-profile", async event => {
  const { kind, name } = event.detail;
  const open = kind === "users" ? openLinkedUser : openLinkedEntity;
  activateTab(kind, false);
  loadedTabs.add(kind);
  const token = Symbol(kind);
  tabLoadTokens.set(kind, token);
  if (!await open(name) && tabLoadTokens.get(kind) === token) loadedTabs.delete(kind);
});
initUsers();
initEntities();
initRecords();
initWorkflows();
initInspections();
// Coordinación solo trabaja en Mis Inspecciones (lista y calendario).
requestJson("/api/session").then(({ user }) => {
  if (user && user.role !== "admin" && user.workflow_role === "coordinacion") {
    tabs.forEach(tab => tab.classList.toggle("hidden", tab.dataset.tab !== "inspections"));
    document.getElementById("inspection-export").classList.add("hidden");
    document.getElementById("workflow-current-user").textContent = `${user.display_name} (Coordinación)`;
    activateTab("inspections");
  } else {
    activateTab("workflows");
  }
}).catch(() => activateTab("workflows"));
async function refreshCatalogStatus() {
  const label = document.getElementById("catalog-status");
  try {
    const status = await requestJson("/api/status");
    const text = status.catalog_running
      ? `${status.catalog_message}: ${status.catalog_processed} archivo(s)`
      : status.catalog_errors
      ? `${status.catalog_message} · ${status.catalog_errors} error(es)`
      : status.catalog_message;
    label.textContent = [
      text,
      !status.catalog_running && lastUpdateText(status.catalog_last_finished),
      status.auto_refresh_minutes
        ? `automática cada ${status.auto_refresh_minutes} min`
        : "",
    ].filter(Boolean).join(" · ");
  } catch (error) {
    label.textContent = `No se pudo consultar el catálogo: ${error.message}`;
  }
}

refreshCatalogStatus();
window.setInterval(refreshCatalogStatus, 10000);
