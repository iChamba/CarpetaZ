import assert from "node:assert/strict";
import test from "node:test";
import { openLinkedUser } from "../static/js/users.js";
import { loadEntities, openLinkedEntity } from "../static/js/entities.js";

class FakeElement {
  constructor(text = "") {
    this.value = "";
    this.disabled = false;
    this.dataset = {};
    this.classList = { toggle() {} };
    this.replaceChildren();
    this.textContent = text;
  }
  set textContent(value) {
    this.text = value;
    this.children = [];
  }
  get textContent() {
    return this.text + this.children.map(child => child.textContent).join("");
  }
  get lastElementChild() {
    return this.children.at(-1);
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.text = "";
    this.children = children;
  }
  add(child) {
    this.append(child);
  }
}

const elements = new Map();
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new FakeElement());
    return elements.get(id);
  },
  createElement() { return new FakeElement(); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
globalThis.Option = class extends FakeElement {
  constructor(text, value) {
    super(text);
    this.value = value;
  }
};

function response(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function installFetch(delay = null) {
  globalThis.fetch = async url => {
    const request = new URL(url, "http://localhost");
    const name = request.searchParams.get("name") || request.searchParams.get("q");
    if (name === "Lenta" && delay) await delay;
    const path = request.pathname;
    if ((path === "/api/user" || path === "/api/entity") && name === "Ausente") {
      return response({ error: "Ficha no encontrada" }, 404);
    }
    if (path === "/api/users" || path === "/api/entities") {
      return response({
        items: [], offset: 0, total: 210, warnings: 0,
        practices: [], departments: [], localities: [], catalog: { ready: true },
      });
    }
    if (path === "/api/user") {
      return response({
        user: { name, document_count: 0, imported_count: 0 },
        authorization_files: [], authorization_records: [], records: [], files: [],
        offset: 0, limit: 100,
      });
    }
    if (path === "/api/entity") {
      return response({
        entity: { name, practice_count: 0, area_count: 0, document_count: 0 },
        practices: [],
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
}

test("linked profiles open outside the current page and clear incompatible filters", async () => {
  installFetch();
  document.getElementById("user-authorization-filter").value = "without";
  document.getElementById("user-status-filter").disabled = true;
  document.getElementById("user-practice-filter").disabled = true;
  assert.equal(await openLinkedUser("Carpeta & nombre"), true);
  assert.match(document.getElementById("user-profile").textContent, /Carpeta & nombre/);
  assert.equal(document.getElementById("user-authorization-filter").value, "all");
  assert.equal(document.getElementById("user-status-filter").disabled, false);
  assert.equal(document.getElementById("user-practice-filter").disabled, false);

  document.getElementById("entity-license-filter").value = "without";
  document.getElementById("entity-license-status-filter").disabled = true;
  assert.equal(await openLinkedEntity("Institución vinculada"), true);
  assert.match(document.getElementById("entity-profile").textContent, /Institución vinculada/);
  assert.equal(document.getElementById("entity-license-filter").value, "");
  assert.equal(document.getElementById("entity-license-status-filter").disabled, false);
});

test("a later navigation wins over an older entity list request", async () => {
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  installFetch(delayed);
  const oldList = loadEntities();
  const slow = openLinkedEntity("Lenta");
  const fast = openLinkedEntity("Rápida");
  assert.equal(await fast, true);
  release();
  await Promise.all([oldList, slow]);
  assert.match(document.getElementById("entity-profile").textContent, /Rápida/);
});

test("a later user navigation wins over an older request", async () => {
  let release;
  const delayed = new Promise(resolve => { release = resolve; });
  installFetch(delayed);
  const slow = openLinkedUser("Lenta");
  const fast = openLinkedUser("Nueva");
  assert.equal(await fast, true);
  release();
  assert.equal(await slow, false);
  assert.match(document.getElementById("user-profile").textContent, /Nueva/);
});

test("missing profiles show the error instead of the previously opened profile", async () => {
  installFetch();
  assert.equal(await openLinkedEntity("Ausente"), false);
  assert.match(document.getElementById("entity-message").textContent, /Ficha no encontrada/);
  assert.doesNotMatch(document.getElementById("entity-profile").textContent, /Rápida/);
  assert.equal(await openLinkedUser("Ausente"), false);
  assert.match(document.getElementById("user-message").textContent, /Ficha no encontrada/);
  assert.doesNotMatch(document.getElementById("user-profile").textContent, /Nueva/);
});

test("a failed directory request stops the loading state and reports the error", async () => {
  globalThis.fetch = async () => response({ error: "Catálogo inaccesible" }, 503);
  assert.equal(await openLinkedUser("Carpeta"), false);
  assert.match(document.getElementById("user-message").textContent, /Catálogo inaccesible/);
  assert.match(document.getElementById("user-profile").textContent, /No se pudo abrir/);
  assert.equal(await openLinkedEntity("Institución"), false);
  assert.match(document.getElementById("entity-message").textContent, /Catálogo inaccesible/);
  assert.match(document.getElementById("entity-profile").textContent, /No se pudo abrir/);
});
