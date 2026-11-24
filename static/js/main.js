import { requestJson } from "./common.js";
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
activateTab("workflows");
async function refreshCatalogStatus() {
  const label = document.getElementById("catalog-status");
  try {
    const status = await requestJson("/api/status");
    label.textContent = status.catalog_running
      ? `${status.catalog_message}: ${status.catalog_processed} archivo(s)`
      : status.catalog_errors
      ? `${status.catalog_message} · ${status.catalog_errors} error(es)`
      : status.catalog_message;
  } catch (error) {
    label.textContent = `No se pudo consultar el catálogo: ${error.message}`;
  }
}

refreshCatalogStatus();
window.setInterval(refreshCatalogStatus, 10000);
