import assert from "node:assert/strict";
import test from "node:test";
import { lastUpdateText } from "../static/js/common.js";

test("muestra hace cuánto se actualizó el catálogo", () => {
  const now = new Date("2026-11-24T15:00:00");
  assert.equal(lastUpdateText("", now), "");
  assert.equal(lastUpdateText("fecha inválida", now), "");
  assert.equal(lastUpdateText("2026-11-24T14:59:30", now), "actualizado hace menos de 1 min");
  assert.equal(lastUpdateText("2026-11-24T14:45:00", now), "actualizado hace 15 min");
  assert.equal(lastUpdateText("2026-11-24T12:30:00", now), "actualizado hace 2 h 30 min");
});
