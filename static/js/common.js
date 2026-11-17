export function setMessage(id, text = "", kind = "") {
  const target = document.getElementById(id);
  target.textContent = text;
  target.className = `notice${kind ? ` ${kind}` : ""}`;
}

export async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    if (!response.ok) throw new Error(`Error HTTP ${response.status}`);
    return response;
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Error HTTP ${response.status}`);
  return data;
}

export function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function makeButton(label, action, className = "button secondary small") {
  const button = element("button", label, className);
  button.type = "button";
  button.dataset.action = action;
  return button;
}

export function appendYearGroups(body, files, colspan, buildRow) {
  const groups = new Map();
  for (const file of files) {
    const year = file.created && !file.created.startsWith("1970") ? file.created.slice(0, 4) : "Sin fecha";
    if (!groups.has(year)) groups.set(year, []);
    groups.get(year).push(file);
  }
  let first = true;
  for (const [year, items] of groups) {
    const header = document.createElement("tr");
    header.className = "year-row";
    const cell = element("td", undefined, "year-cell");
    cell.colSpan = colspan;
    const label = element("button", undefined, "year-toggle");
    label.type = "button";
    const rows = items.map(buildRow);
    const setExpanded = expanded => {
      label.textContent = `${expanded ? "▾" : "▸"} ${year} (${items.length})`;
      label.setAttribute("aria-expanded", String(expanded));
      rows.forEach(row => row.classList.toggle("hidden", !expanded));
    };
    label.addEventListener("click", () => setExpanded(label.getAttribute("aria-expanded") !== "true"));
    setExpanded(first);
    first = false;
    cell.append(label);
    header.append(cell);
    body.append(header, ...rows);
  }
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
