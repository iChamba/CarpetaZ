import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("../static/styles.css", import.meta.url), "utf8");
const open = ".preview-browser-layout:not(.preview-hidden)";

test("con la vista previa abierta, la tabla de documentos de entidad entra en la lista", () => {
  // Sin esto la tabla tenía 760px de ancho mínimo y los botones Ver quedaban fuera de la vista.
  assert.match(css, new RegExp(`${open.replace(/[.()]/g, "\\$&")} \\.preview-file-list \\.entity-documents-wrap table \\{ min-width: 0; \\}`));
  assert.match(css, /:not\(\.preview-hidden\) \.entity-documents-wrap :is\(th, td\):is\(:nth-child\(3\), :nth-child\(4\)\) \{ display: none; \}/);
  assert.match(css, /:not\(\.preview-hidden\) \.entity-documents-wrap td \{ overflow-wrap: anywhere; \}/);
});

test("con la vista previa cerrada, la tabla conserva su ancho completo", () => {
  assert.match(css, /\.preview-file-list \.entity-documents-wrap table \{ min-width: 760px; \}/);
});
