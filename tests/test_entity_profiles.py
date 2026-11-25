import os
import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

_ENV_ROOT = tempfile.TemporaryDirectory(prefix="carpetaz-entity-profile-env-")
os.environ.setdefault("DOCUMENT_ROOT", _ENV_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(_ENV_ROOT.name) / "appdata"))

import database
import inspection_planning
import settings
from routes.entity_profiles import EntityProfileRoutes, name_match_kind, names_match
from routes.inspections import InspectionRoutes
from routes.planning import PlanningRoutes


class Handler(EntityProfileRoutes, PlanningRoutes, InspectionRoutes):
    def __init__(self, user):
        self.current_user = user
        self.response = None
        self.wfile = BytesIO()

    def send_json(self, payload, status=200):
        self.response = status, payload

    def send_error_json(self, message, status=400):
        self.response = status, {"error": message}


AREAS = ([{"institution": "A.N.C.A.P. - PORTLAND"}, {"institution": "Hospital Uno"}], 0)


class EntityProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "5 Registro de tramites y planillas" / "INSPECCIONES"
        root.mkdir(parents=True)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Hoja1"
        for row in (
            [], [], [None, "Institución", "Departamento", "Equipos", "Última inspección", "Próxima inspección"],
            [None, "ANCAP Portland", "Lavalleja", "Medidor", datetime(2025, 1, 1), datetime(2027, 1, 1)],
            [None, "ANCAP Portland", "Lavalleja", "Irradiador", datetime(2024, 1, 1), datetime(2026, 1, 1)],
            [None, "Otra Empresa", "Salto", "Medidor", datetime(2025, 1, 1), datetime(2027, 1, 1)],
        ):
            sheet.append(row)
        workbook.save(root / "PL-11-18 Planilla de irradiadores.xlsx")
        inspection_planning._cache.clear()
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.temp.name) / "test.sqlite3"
        database.initialize_db()
        connection = database.connect_db()
        self.users = {}
        for name, role in (("chief", "jefe_inspeccion"), ("inspector", "inspeccion"),
                           ("secretary", "administracion")):
            cursor = connection.execute(
                "INSERT INTO workflow_users (username, display_name, password_hash, role, "
                "workflow_role, active, created_at) VALUES (?, ?, 'hash', 'user', ?, 1, '2026-01-01')",
                (name, name, role),
            )
            self.users[name] = {"id": cursor.lastrowid, "username": name, "display_name": name,
                                "role": "user", "workflow_role": role}
        connection.commit()
        connection.close()
        self.patches = [
            mock.patch.object(settings, "PLANNING_ROOT", root.parent),
            mock.patch("routes.inspections._entity_areas", return_value=AREAS),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        database.DATABASE = self.original_database
        inspection_planning._cache.clear()
        self.temp.cleanup()

    def call(self, user, method, *args):
        handler = Handler(self.users[user])
        getattr(handler, method)(*args)
        return handler

    def record(self, user="inspector", name="A.N.C.A.P. - PORTLAND"):
        handler = self.call(user, "get_entity_record", {"name": [name]})
        self.assertEqual(handler.response[0], 200)
        return handler.response[1]

    def test_names_match_ignores_punctuation(self):
        self.assertTrue(names_match("A.N.C.A.P. - PORTLAND", "ANCAP Portland"))
        self.assertTrue(names_match("Hospital Uno", "Hospital Uno - Sede Centro"))
        self.assertFalse(names_match("COR", "COR Litoral"))
        self.assertFalse(names_match("Otra Empresa", "ANCAP Portland"))
        # Nombres genéricos no alcanzan; por palabras completas y solo exactos vienen marcados.
        self.assertEqual(name_match_kind("Sanatorio Americano", "Sanatorio"), "")
        self.assertEqual(name_match_kind("Centro Médico", "Centro Médico Sur"), "")
        self.assertEqual(name_match_kind("Hospital Uno", "Hospital Uno - Sede Centro"), "partial")
        self.assertEqual(name_match_kind("A.N.C.A.P. - PORTLAND", "ANCAP Portland"), "exact")
        self.assertEqual(name_match_kind("Portland", "ANCAP Portlandia"), "")

    def test_profile_is_edited_with_history_and_permissions(self):
        empty = self.record()
        self.assertEqual(empty["profile"]["version"], 0)
        self.assertFalse(empty["can_edit"])
        denied = self.call("inspector", "update_entity_record", {"name": "A.N.C.A.P. - PORTLAND", "version": 0})
        self.assertEqual(denied.response[0], 403)
        saved = self.call("secretary", "update_entity_record", {
            "name": "a.n.c.a.p. - portland", "version": 0, "rut": "210000000016",
            "phone": "4442 4594", "email": "contacto@ancap.com.uy", "notes": "Portería en ruta 8",
        }).response[1]
        self.assertEqual(saved["profile"]["rut"], "210000000016")
        self.assertEqual(saved["profile"]["version"], 2)
        self.assertIn("RUT: (vacío) → 210000000016", saved["events"][0]["details_text"])
        stale = self.call("secretary", "update_entity_record", {"name": "A.N.C.A.P. - PORTLAND", "version": 0})
        self.assertEqual(stale.response[0], 409)
        with self.assertRaises(ValueError):
            self.call("secretary", "update_entity_record", {
                "name": "A.N.C.A.P. - PORTLAND", "version": 2, "email": "sin-arroba",
            })

    def test_inspection_fills_only_empty_profile_fields(self):
        self.call("secretary", "update_entity_record", {
            "name": "A.N.C.A.P. - PORTLAND", "version": 0, "phone": "4442 4594",
        })
        created = self.call("secretary", "create_inspection", {
            "entity_name": "A.N.C.A.P. - PORTLAND", "rut": "210000000016",
            "phone": "099 000 000", "address": "Ruta 8 km 113", "city": "Minas",
        })
        self.assertEqual(created.response[0], 201)
        record = self.record()
        self.assertEqual(record["profile"]["phone"], "4442 4594")
        self.assertEqual(record["profile"]["rut"], "210000000016")
        self.assertEqual(record["profile"]["address"], "Ruta 8 km 113")
        self.assertEqual(record["events"][0]["action"], "Completada desde una inspección")
        self.assertIn(f"Inspección: #{created.response[1]['id']}", record["events"][0]["details_text"])
        # Asociar la entidad después también completa la ficha.
        later = self.call("secretary", "create_inspection", {"email": "otro@hospital.uy"})
        self.call("secretary", "act_on_inspection", {
            "inspection_id": later.response[1]["id"], "version": 0, "action": "set_entity",
            "entity_name": "Hospital Uno",
        })
        self.assertEqual(self.record(name="Hospital Uno")["profile"]["email"], "otro@hospital.uy")

    def test_equipment_suggested_linked_and_unlinked(self):
        self.call("chief", "get_inspection_planning")
        # La planificación (equipos y vencimientos) solo la ven Jefe de Inspección y administradores.
        for user in ("inspector", "secretary"):
            hidden = self.record(user)
            self.assertEqual((hidden["equipment"], hidden["suggested_equipment"]), ([], []), user)
            self.assertFalse(hidden["can_view_equipment"], user)
        record = self.record("chief")
        self.assertTrue(record["can_view_equipment"])
        self.assertEqual(record["equipment"], [])
        suggested = record["suggested_equipment"]
        self.assertEqual({item["practice"] for item in suggested}, {"Medidor", "Irradiador"})
        self.assertEqual({item["match"] for item in suggested}, {"exact"})
        self.assertEqual(suggested[0]["practice"], "Irradiador")
        ids = [item["id"] for item in suggested]
        denied = self.call("inspector", "link_entity_equipment", {"name": "A.N.C.A.P. - PORTLAND", "item_ids": ids})
        self.assertEqual(denied.response[0], 403)
        linked = self.call("chief", "link_entity_equipment", {
            "name": "A.N.C.A.P. - PORTLAND", "item_ids": ids,
        }).response[1]
        self.assertEqual(len(linked["equipment"]), 2)
        self.assertEqual(linked["suggested_equipment"], [])
        self.assertEqual(linked["equipment"][0]["status"], "vencida")
        self.assertEqual(linked["events"][0]["action"], "Equipos asociados")
        # El equipo queda con la entidad en la planificación y no se puede pasar a otra sin quitarla.
        planning = self.call("chief", "get_inspection_planning").response[1]["items"]
        self.assertEqual({item["entity_name"] for item in planning if item["id"] in ids}, {"A.N.C.A.P. - PORTLAND"})
        with self.assertRaises(ValueError):
            self.call("chief", "link_entity_equipment", {"name": "Hospital Uno", "item_ids": ids[:1]})
        # Una acción desconocida no desasocia nada.
        with self.assertRaises(ValueError):
            self.call("chief", "link_entity_equipment", {
                "name": "A.N.C.A.P. - PORTLAND", "item_ids": ids, "action": "borrar",
            })
        self.assertEqual(len(self.record("chief")["equipment"]), 2)
        unlinked = self.call("chief", "link_entity_equipment", {
            "name": "A.N.C.A.P. - PORTLAND", "item_ids": ids[:1], "action": "unlink",
        }).response[1]
        self.assertEqual(len(unlinked["equipment"]), 1)
        self.assertEqual(len(unlinked["suggested_equipment"]), 1)


if __name__ == "__main__":
    unittest.main()
