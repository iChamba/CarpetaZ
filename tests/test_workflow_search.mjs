import assert from "node:assert/strict";
import test from "node:test";
import { matchesWorkflowSearch, workflowStatusGroup } from "../static/js/workflows.js";

test("search matches names, institutions, numbers, codes and responsible people", () => {
  const individual = {
    id: 17, workflow_name: "Autorización Individual", applicant_first_name: "María",
    applicant_last_name: "Pérez", identity_number: "00123456", form_number: "F-88",
    short_code: "CTI-123", assigned_code: "AS-90", assigned_user_name: "Lucía",
  };
  for (const query of ["maria perez", "individual", "00123456", "f-88", "cti-123",
    "as-90", "lucia", "17", "perez f-88"]) {
    assert.equal(matchesWorkflowSearch(individual, query), true, query);
  }
  assert.equal(matchesWorkflowSearch(individual, "maria inexistente"), false);
  const entity = {
    workflow_name: "Licencia de Operación", applicant_company_name: "Clínica del Norte",
    form_number: "007", entity_code: "ABC-42", linked_entity_name: "Centro de salud",
  };
  for (const query of ["clinica", "norte 007", "abc-42", "centro salud", "operacion"]) {
    assert.equal(matchesWorkflowSearch(entity, query), true, query);
  }
  assert.equal(matchesWorkflowSearch(entity, "xyz"), false);
});

test("identity search ignores dots, dashes and spaces without losing leading zeros", () => {
  const plain = { identity_number: "00123456", applicant_first_name: "María" };
  const formatted = { identity_number: "00.123.456-7" };
  for (const query of ["00.123.456", "00-123-456", "00 123 456", "maria 00.123.456"]) {
    assert.equal(matchesWorkflowSearch(plain, query), true, query);
  }
  for (const query of ["001234567", "00.123.456-7", "00 123 456 7"]) {
    assert.equal(matchesWorkflowSearch(formatted, query), true, query);
  }
  assert.equal(matchesWorkflowSearch(formatted, "001234568"), false);
  assert.equal(matchesWorkflowSearch({ identity_number: "" }, "00.123.456"), false);
  assert.equal(matchesWorkflowSearch(plain, "maria 00.123.999"), false);
});

test("workflow status groups distinguish active, observed, rejected, completed and delivered cases", () => {
  const active = { workflow_name: "Autorización Individual", status: "active" };
  assert.equal(workflowStatusGroup(active), "active");
  assert.equal(workflowStatusGroup({ ...active, is_observed: 1 }), "observed");
  assert.equal(workflowStatusGroup({ ...active, status: "completed", stage_key: "rechazado" }), "rejected");
  assert.equal(workflowStatusGroup({ ...active, status: "completed", stage_key: "finalizado" }), "completed");
  assert.equal(workflowStatusGroup({ ...active, status: "completed", delivered_at: "2026-01-01" }), "delivered");
  assert.equal(workflowStatusGroup({
    workflow_name: "Licencia de Operación", is_entity_workflow: 1, status: "completed", stage_key: "rechazado",
  }), "rejected");
  assert.equal(workflowStatusGroup({
    workflow_name: "Otro flujo", status: "completed", stage_key: "finalizado",
  }), "completed");
});
