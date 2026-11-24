import assert from "node:assert/strict";
import test from "node:test";
import { workflowDeadline } from "../static/js/workflows.js";
import { workflowRecordsSummary } from "../static/js/records.js";

const created = Date.parse("2026-01-01T12:00:00Z");
const day = 86400 * 1000;
const active = {
  workflow_name: "Autorización Individual",
  created_at: "2026-01-01T12:00:00Z",
  deadline_started_at: "2026-01-01T12:00:00Z",
  deadline_paused_at: null,
  status: "active",
};

test("individual and other workflows use 30 and 60 continuous days", () => {
  assert.deepEqual(workflowDeadline(active, created), {
    days: 30, dueAt: created + 30 * day, text: "Restan: 30 d 0 h 0 min", tone: "on-time",
  });
  const other = workflowDeadline({ ...active, workflow_name: "Licencia de Operación" }, created + day);
  assert.equal(other.days, 60);
  assert.equal(other.dueAt, created + 60 * day);
  assert.equal(other.text, "Restan: 59 d 0 h 0 min");
});

test("deadline counts down to the exact instant and then reports overdue time", () => {
  assert.equal(workflowDeadline(active, created + 30 * day - 500).text, "Restan: 0 d 0 h 1 min");
  assert.equal(workflowDeadline(active, created + 30 * day).tone, "overdue");
  assert.equal(workflowDeadline(active, created + 31 * day + 60000).text, "Vencido: 1 d 0 h 1 min");
});

test("warning starts exactly 5 or 10 days before the deadline", () => {
  const entity = { ...active, workflow_name: "Licencia de Construcción" };
  assert.equal(workflowDeadline(active, created + 25 * day - 1).tone, "on-time");
  assert.equal(workflowDeadline(active, created + 25 * day).tone, "warning");
  assert.equal(workflowDeadline(entity, created + 50 * day - 1).tone, "on-time");
  assert.equal(workflowDeadline(entity, created + 50 * day).tone, "warning");
});

test("completed and rejected cases show total time through completion", () => {
  const completed = {
    ...active, status: "completed", completed_at: new Date(created + 20 * day + 3 * 3600000 + 25 * 60000 + 59000).toISOString(),
  };
  assert.equal(workflowDeadline(completed, created + 100 * day).text,
    "Tiempo total: 20 d 3 h 25 min · Dentro de plazo");
  assert.equal(workflowDeadline(completed, created + 200 * day).text,
    "Tiempo total: 20 d 3 h 25 min · Dentro de plazo");
  const rejected = {
    ...completed, stage_key: "rechazado", completed_at: new Date(created + 32 * day).toISOString(),
  };
  const result = workflowDeadline(rejected, created + 100 * day);
  assert.equal(result.text, "Tiempo total: 32 d 0 h 0 min · Fuera de plazo");
  assert.equal(result.tone, "overdue");
  const sameInstant = { ...completed, completed_at: completed.created_at };
  assert.equal(workflowDeadline(sameInstant).text, "Tiempo total: 0 d 0 h 0 min · Dentro de plazo");
});

test("invalid or missing dates do not produce a misleading countdown", () => {
  assert.equal(workflowDeadline({ ...active, created_at: "" }, created).text,
    "Plazo no disponible: fecha inválida");
  assert.equal(workflowDeadline({ ...active, status: "completed", completed_at: null }, created).dueAt, null);
  assert.equal(workflowDeadline({
    ...active, status: "completed", completed_at: new Date(created - 1).toISOString(),
  }).dueAt, null);
});

test("observation freezes the remaining or overdue time without showing a due date", () => {
  const paused = { ...active, deadline_paused_at: new Date(created + 10 * day).toISOString() };
  assert.deepEqual(workflowDeadline(paused, created + 100 * day), {
    days: 30, dueAt: null, text: "Pausado · Restan: 20 d 0 h 0 min", tone: "on-time",
  });
  const overdue = { ...paused, deadline_paused_at: new Date(created + 31 * day).toISOString() };
  assert.equal(workflowDeadline(overdue, created + 100 * day).text, "Pausado · Vencido: 1 d 0 h 0 min");
});

test("return to inspection resets the full deadline and final duration", () => {
  const reset = { ...active, deadline_started_at: new Date(created + 15 * day).toISOString() };
  assert.equal(workflowDeadline(reset, created + 15 * day).text, "Restan: 30 d 0 h 0 min");
  assert.equal(workflowDeadline({ ...reset, workflow_name: "Licencia de Operación" }, created + 15 * day).text,
    "Restan: 60 d 0 h 0 min");
  const completed = { ...reset, status: "completed", completed_at: new Date(created + 17 * day).toISOString() };
  assert.equal(workflowDeadline(completed, created + 200 * day).text, "Tiempo total: 2 d 0 h 0 min · Dentro de plazo");
});

test("invalid pause and reset timestamps do not produce a misleading deadline", () => {
  for (const patch of [
    { deadline_started_at: null }, { deadline_started_at: new Date(created - day).toISOString() },
    { deadline_paused_at: "invalid" }, { deadline_paused_at: new Date(created - day).toISOString() },
    { deadline_paused_at: new Date(created + 2 * day).toISOString() },
  ]) {
    assert.equal(workflowDeadline({ ...active, ...patch }, created + day).tone, "unavailable");
  }
});

test("records summary counts each workflow by status and active deadline", () => {
  const now = created + 30 * day;
  const cases = [
    { ...active, deadline_started_at: new Date(created + 10 * day).toISOString() },
    { ...active, deadline_started_at: new Date(created + 5 * day).toISOString() },
    active,
    { ...active, deadline_started_at: null },
    { ...active, is_observed: 1, deadline_paused_at: new Date(created + day).toISOString() },
    { ...active, status: "completed", stage_key: "rechazado" },
    { ...active, status: "completed", stage_key: "finalizado" },
    { ...active, status: "completed", stage_key: "finalizado", delivered_at: "2026-02-01" },
  ];
  assert.deepEqual(workflowRecordsSummary(cases, true, now), {
    "on-time": 1, warning: 1, overdue: 1, unavailable: 1,
    observed: 1, rejected: 1, completed: 1, delivered: 1,
  });
  assert.deepEqual(workflowRecordsSummary(cases, false, now), {
    "on-time": 0, warning: 0, overdue: 0, unavailable: 4,
    observed: 1, rejected: 1, completed: 1, delivered: 1,
  });
});
