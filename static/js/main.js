import { initSearch, refreshIndexStatus } from "./search.js";
import { initRecords, loadRecords } from "./records.js";
import { initUsers, invalidateLoadedUsers, loadUsers } from "./users.js";
import { initEntities, loadEntities } from "./entities.js";
import { initWorkbooks, loadWorkbooks } from "./workbooks.js";

const tabs = [...document.querySelectorAll(".tab")];
const panes = [...document.querySelectorAll(".pane")];
const loadedTabs = new Set();

window.addEventListener("records-stale", () => loadedTabs.delete("records"));
window.addEventListener("users-stale", () => {
  loadedTabs.delete("users");
  invalidateLoadedUsers();
});

function activateTab(name) {
  tabs.forEach(tab => tab.classList.toggle("active", tab.dataset.tab === name));
  panes.forEach(pane => pane.classList.toggle("active", pane.id === `pane-${name}`));
  if (name === "users") loadTabOnce(name, loadUsers);
  if (name === "entities") loadTabOnce(name, loadEntities);
  if (name === "records") loadTabOnce(name, loadRecords);
  if (name === "workbooks") loadTabOnce(name, loadWorkbooks);
}

function loadTabOnce(name, loader) {
  if (loadedTabs.has(name)) return;
  loadedTabs.add(name);
  loader().then(loaded => {
    if (loaded === false) loadedTabs.delete(name);
  });
}

tabs.forEach(tab => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));

initSearch();
initUsers();
initEntities();
initRecords();
initWorkbooks();
activateTab("users");
refreshIndexStatus();
window.setInterval(refreshIndexStatus, 10000);
