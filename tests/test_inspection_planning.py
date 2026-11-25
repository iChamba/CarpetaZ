import os
import sqlite3
import tempfile
from io import BytesIO
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from openpyxl import Workbook, load_workbook

_ENV_ROOT = tempfile.TemporaryDirectory(prefix="carpetaz-planning-env-")
os.environ.setdefault("DOCUMENT_ROOT", _ENV_ROOT.name)
os.environ.setdefault("APP_DATA_DIR", str(Path(_ENV_ROOT.name) / "appdata"))

import database
import inspection_planning
import settings
from inspection_planning import filter_groups, load_planning, parse_date
from routes.inspections import InspectionRoutes
from routes.planning import PlanningRoutes


class Handler(PlanningRoutes, InspectionRoutes):
    def __init__(self, user):
        self.current_user = user
        self.response = None
        self.wfile = BytesIO()
        self.headers_sent = {}

    def send_response(self, status):
        self.response = status, None

    def send_header(self, name, value):
        self.headers_sent[name] = value

    def end_headers(self):
        pass

    def send_json(self, payload, status=200):
        self.response = status, payload

    def send_error_json(self, message, status=400):
        self.response = status, {"error": message}

TODAY = date(2026, 11, 24)


def write(path: Path, sheets: dict):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "5 Registro de tramites y planillas"
        folder = self.root / "INSPECCIONES"
        inspection_planning._cache.clear()
        # Médicos: encabezado en dos filas, la próxima sale de "nuevo vencimiento".
        write(folder / "PL-11-14 Planilla de equipos MÉDICOS y ODONTOLÓGICOS.xlsx", {
            "Médicos": [
                [], [],
                ["Institución", "Dirección", "Depto", "Tipo de equipo", "última inspección",
                 "próxima inspección", None, None, None],
                [None, None, None, None, None, "mes", "año", "próxima insp", "nuevo vencimiento 2023"],
                ["Hospital Uno", "Calle 1", "Montevideo", "Arco en C", datetime(2024, 7, 23),
                 7, 27, datetime(2025, 7, 23), datetime(2027, 7, 23)],
                ["Hospital Uno", "Calle 1", "Montevideo", "Arco en C", datetime(2022, 1, 10),
                 1, 25, datetime(2025, 1, 10), None],
                ["Hospital Uno", "Calle 1", "Montevideo", "Mamógrafo", datetime(2020, 5, 5),
                 5, 23, None, datetime(2023, 5, 5)],
                ["Clínica Vieja", "Calle 4", "Rivera", "RX portátil", datetime(2022, 3, 3),
                 3, 25, datetime(2025, 3, 3), None],
                ["Clínica Baja", "Calle 2", "Salto", "RX fijo", datetime(2021, 1, 1),
                 None, None, None, "Equipo de baja"],
            ],
            "Odontológicos": [
                [], [],
                [None, None, None, None, None, "Próxima inspección"],
                ["Institución", "Dirección", "Depto", "Tipo de equipo", "Última inspección"],
                [None, None, None, None, None, "mes", "año", "nuevo vencimiento 2023"],
                ["Consultorio Dos", "Calle 3", "Colonia", "RXO", datetime(2024, 6, 11), 6, 34, None],
            ],
            "Hoja2": [["Etiquetas de fila"], ["Hospital Uno"]],
        })
        # Industriales: institución combinada, FECHA: debajo de ÚLTIMA INSPECCIÓN, BAJA.
        write(folder / "PL-11-16 Planilla de instalaciones INDUSTRIALES.xlsx", {
            "MEDIDORES": [
                [], [],
                ["USUARIO:", "DIRECCIÓN", "DEPARTAMENTO:", None, "ÚLTIMA  INSPECCIÓN:", "PRÓXIMA INSPECCIÓN:", "ACTUALIZADO"],
                [None, "DE INSTALACIÓN:", None, "FECHA:", "C/OBS:", None, "HOY:"],
                ["Fábrica", "Ruta 8", "LAVALLEJA", datetime(2025, 11, 1), None, datetime(2026, 12, 1), datetime(2026, 11, 20)],
                [None, None, None, datetime(2017, 8, 31), "BAJA", "NO CORRESPONDE", datetime(2026, 11, 20)],
                ["Puerto", "Rambla", "MONTEVIDEO", datetime(2020, 1, 1), None, datetime(2022, 1, 1), datetime(2026, 11, 20)],
                ["@SUMA(A1)", "Calle", "SALTO", datetime(2019, 1, 1), None, datetime(2021, 1, 1), None],
            ],
        })
        write(folder / "PL-11-17 Planilla de instalaciones de RADIOTERAPIA.xlsx", {
            "instalaciones": [
                [], [],
                [None, "Instalación", " insp", "vencimiento inspección  AL", "vencimiento inspección  HDR"],
                [1, "COR Litoral", "set-25", datetime(2026, 9, 1), "n/a"],
            ],
        })
        write(folder / "PL-11-24 Planilla de seguimiento de observaciones.xlsx", {
            "Hoja1": [["Institución", "Fecha de inspección"], ["No debe aparecer", datetime(2025, 1, 1)]],
        })

    def tearDown(self):
        inspection_planning._cache.clear()
        self.temp.cleanup()

    def groups(self):
        result = load_planning(self.root, TODAY)
        return result, {(group["institution"], group["practice"]): group for group in result["groups"]}

    def test_reads_every_planilla_except_observaciones(self):
        result, _ = self.groups()
        names = [item["file"] for item in result["files"]]
        self.assertEqual(len(names), 3)
        self.assertFalse(any("observaciones" in name for name in names))
        self.assertEqual(result["errors"], [])

    def test_groups_by_institution_and_practice_with_last_and_next(self):
        _, groups = self.groups()
        arco = groups[("Hospital Uno", "Arco en C")]
        self.assertEqual(arco["equipment_count"], 2)
        self.assertEqual(arco["last_inspection"], "2024-07-23")
        self.assertEqual(arco["next_inspection"], "2027-07-23")
        self.assertEqual(arco["status"], "al_dia")
        self.assertEqual(arco["department"], "Montevideo")
        mamografo = groups[("Hospital Uno", "Mamógrafo")]
        self.assertEqual(mamografo["next_inspection"], "2023-05-05")
        self.assertEqual(mamografo["status"], "vencida")
        self.assertNotIn(("Clínica Baja", "RX fijo"), groups)

    def test_medicos_y_odontologicos_only_use_nuevo_vencimiento(self):
        _, groups = self.groups()
        # Sin "nuevo vencimiento" no se usa ni el mes/año ni la próxima inspección anterior.
        self.assertEqual(groups[("Consultorio Dos", "RXO")]["next_inspection"], "")
        self.assertEqual(groups[("Consultorio Dos", "RXO")]["status"], "sin_fecha")
        self.assertEqual(groups[("Clínica Vieja", "RX portátil")]["next_inspection"], "")

    def test_industrial_sheet_uses_fecha_column_and_skips_baja(self):
        _, groups = self.groups()
        factory = groups[("Fábrica", "Medidores")]
        self.assertEqual(factory["equipment_count"], 1)
        self.assertEqual(factory["last_inspection"], "2025-11-01")
        self.assertEqual(factory["next_inspection"], "2026-12-01")
        self.assertEqual(factory["status"], "proxima")
        self.assertEqual(factory["categories"], ["Industriales"])
        self.assertEqual(groups[("Puerto", "Medidores")]["status"], "vencida")

    def test_generic_sheet_uses_category_and_text_dates(self):
        _, groups = self.groups()
        cor = groups[("COR Litoral", "Radioterapia")]
        self.assertEqual(cor["last_inspection"], "2025-09-01")
        self.assertEqual(cor["next_inspection"], "2026-09-01")

    def test_parse_date_ignores_excel_filler_dates(self):
        self.assertIsNone(parse_date(datetime(1904, 12, 29)))
        self.assertEqual(parse_date("15/03/2024"), date(2024, 3, 15))
        self.assertIsNone(parse_date("n/a"))

    def test_missing_folder_returns_empty_planning(self):
        result = load_planning(Path(self.temp.name) / "no existe", TODAY)
        self.assertFalse(result["root_exists"])
        self.assertEqual(result["groups"], [])

    def test_filter_by_month_of_next_inspection(self):
        result, _ = self.groups()
        rows = filter_groups(result["groups"], month="2026-12")
        self.assertEqual({row["institution"] for row in rows}, {"Fábrica"})


class PlanningRouteTests(PlanningTests):
    """Planificación guardada en la base: ediciones, relectura, agenda y Excel."""

    def setUp(self):
        super().setUp()
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.temp.name) / "test.sqlite3"
        database.initialize_db()
        connection = database.connect_db()
        self.users = {}
        for name, role in (("chief", "jefe_inspeccion"), ("inspector", "inspeccion"),
                           ("secretary", "administracion"), ("admin", "")):
            account_role = "admin" if name == "admin" else "user"
            cursor = connection.execute(
                "INSERT INTO workflow_users (username, display_name, password_hash, role, "
                "workflow_role, active, created_at) VALUES (?, ?, 'hash', ?, ?, 1, '2026-01-01')",
                (name, name, account_role, role),
            )
            self.users[name] = {"id": cursor.lastrowid, "username": name, "display_name": name,
                                "role": account_role, "workflow_role": role}
        connection.commit()
        connection.close()
        self.root_patch = mock.patch.object(settings, "PLANNING_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        database.DATABASE = self.original_database
        super().tearDown()

    def call(self, user, method, *args):
        handler = Handler(self.users[user])
        getattr(handler, method)(*args)
        return handler

    def items(self, user="chief"):
        handler = self.call(user, "get_inspection_planning")
        self.assertEqual(handler.response[0], 200)
        return {(item["institution"], item["practice"]): item for item in handler.response[1]["items"]}

    def test_only_chief_or_admin_can_see_planning(self):
        for name, allowed in (("chief", True), ("admin", True), ("inspector", False), ("secretary", False)):
            handler = self.call(name, "get_inspection_planning")
            self.assertEqual(handler.response[0], 200 if allowed else 403, name)
            if allowed:
                self.assertTrue(handler.response[1]["items"])
                self.assertEqual(len(handler.response[1]["sync"]["files"]), 3)

    def test_manual_edits_survive_rereading_and_excel_is_untouched(self):
        excel = self.root / "INSPECCIONES" / "PL-11-14 Planilla de equipos MÉDICOS y ODONTOLÓGICOS.xlsx"
        before = excel.read_bytes()
        item = self.items()[("Hospital Uno", "Arco en C")]
        handler = self.call("chief", "update_planning_item", {
            "id": item["id"], "version": item["version"], "next_inspection": "2027-01-15",
            "department": "Montevideo", "notes": "Coordinar con física médica",
        })
        self.assertEqual(handler.response[0], 200)
        edited = {row["id"]: row for row in handler.response[1]["items"]}[item["id"]]
        self.assertEqual(edited["next_inspection"], "2027-01-15")
        self.assertEqual(edited["edited"], ["next_inspection"])
        self.assertEqual(edited["sheet"]["next_inspection"], "2027-07-23")
        self.assertIn("Próxima inspección: 23/07/2027 → 15/01/2027", edited["events"][0]["details_text"])
        # Volver a leer no pisa la corrección ni las notas.
        synced = self.call("chief", "sync_inspection_planning", {})
        again = {row["id"]: row for row in synced.response[1]["items"]}[item["id"]]
        self.assertEqual(again["next_inspection"], "2027-01-15")
        self.assertEqual(again["notes"], "Coordinar con física médica")
        self.assertEqual(excel.read_bytes(), before)
        # Restaurar vuelve a los datos de las planillas.
        restored = self.call("chief", "update_planning_item", {
            "id": item["id"], "version": again["version"], "restore": True,
        })
        row = {row["id"]: row for row in restored.response[1]["items"]}[item["id"]]
        self.assertEqual(row["next_inspection"], "2027-07-23")
        self.assertEqual(row["edited"], [])
        self.assertEqual(row["notes"], "Coordinar con física médica")

    def test_stale_version_and_invalid_dates_are_rejected(self):
        item = self.items()[("Fábrica", "Medidores")]
        with self.assertRaises(ValueError):
            self.call("chief", "update_planning_item", {
                "id": item["id"], "version": item["version"], "next_inspection": "31/12/2026",
            })
        stale = self.call("chief", "update_planning_item", {
            "id": item["id"], "version": item["version"] + 5, "notes": "x",
        })
        self.assertEqual(stale.response[0], 409)
        denied = self.call("inspector", "update_planning_item", {
            "id": item["id"], "version": item["version"], "notes": "x",
        })
        self.assertEqual(denied.response[0], 403)

    def test_planning_is_reread_when_the_reader_changes(self):
        self.items()
        connection = database.connect_db()
        connection.execute("UPDATE planning_sync SET parser_version = 1")
        connection.execute("UPDATE planning_items SET sheet_next_inspection = '2099-01-01'")
        connection.commit()
        connection.close()
        item = self.items()[("Hospital Uno", "Arco en C")]
        self.assertEqual(item["next_inspection"], "2027-07-23")
        connection = database.connect_db()
        version = connection.execute("SELECT parser_version FROM planning_sync").fetchone()[0]
        connection.close()
        self.assertEqual(version, inspection_planning.PARSER_VERSION)

    def test_rows_missing_from_sheets_are_kept_and_flagged(self):
        self.items()
        (self.root / "INSPECCIONES" / "PL-11-17 Planilla de instalaciones de RADIOTERAPIA.xlsx").unlink()
        synced = self.call("chief", "sync_inspection_planning", {})
        rows = {(row["institution"], row["practice"]): row for row in synced.response[1]["items"]}
        self.assertFalse(rows[("COR Litoral", "Radioterapia")]["in_sheets"])
        self.assertTrue(rows[("Fábrica", "Medidores")]["in_sheets"])

    def test_schedule_inspection_from_planning_links_both(self):
        item = self.items()[("Fábrica", "Medidores")]
        handler = self.call("chief", "create_inspection", {
            "company_name": "Fábrica", "practice": "Medidores", "city": "LAVALLEJA",
            "inspector_id": self.users["inspector"]["id"], "planning_item_id": item["id"],
        })
        self.assertEqual(handler.response[0], 201)
        inspection_id = handler.response[1]["id"]
        row = self.items()[("Fábrica", "Medidores")]
        self.assertEqual([entry["id"] for entry in row["inspections"]], [inspection_id])
        self.assertEqual(row["events"][-1]["action"], "Inspección agendada")
        connection = database.connect_db()
        details = connection.execute(
            "SELECT details FROM inspection_events WHERE inspection_id = ? AND action = 'created'",
            (inspection_id,),
        ).fetchone()[0]
        connection.close()
        self.assertIn("Fábrica · Medidores", details)
        secretary = self.call("secretary", "create_inspection", {"planning_item_id": item["id"]})
        self.assertEqual(secretary.response[0], 403)
        with self.assertRaises(ValueError):
            self.call("chief", "create_inspection", {
                "inspector_id": self.users["inspector"]["id"], "planning_item_id": 99999,
            })

    def entities(self):
        return mock.patch(
            "routes.inspections._entity_areas",
            return_value=([{"institution": "HOSPITAL UNO"}, {"institution": "Fábrica Norte"}], 0),
        )

    def inspection(self, inspection_id):
        connection = database.connect_db()
        row = connection.execute("SELECT * FROM inspections WHERE id = ?", (inspection_id,)).fetchone()
        connection.close()
        return row

    def test_entity_is_saved_when_creating_and_shared_with_planning(self):
        item = self.items()[("Hospital Uno", "Arco en C")]
        with self.entities():
            handler = self.call("chief", "create_inspection", {
                "company_name": "Hospital Uno", "entity_name": "hospital uno",
                "inspector_id": self.users["inspector"]["id"], "planning_item_id": item["id"],
            })
            self.assertEqual(handler.response[0], 201)
            created = self.inspection(handler.response[1]["id"])
            self.assertEqual(created["entity_name"], "HOSPITAL UNO")
            self.assertEqual(self.items()[("Hospital Uno", "Arco en C")]["entity_name"], "HOSPITAL UNO")
            # La siguiente inspección agendada desde esa fila hereda la entidad.
            again = self.call("chief", "create_inspection", {
                "inspector_id": self.users["inspector"]["id"], "planning_item_id": item["id"],
            })
            self.assertEqual(self.inspection(again.response[1]["id"])["entity_name"], "HOSPITAL UNO")
            # Secretaría también puede asociar la entidad al crear desde cero.
            secretary = self.call("secretary", "create_inspection", {"entity_name": "Fábrica Norte"})
            self.assertEqual(self.inspection(secretary.response[1]["id"])["entity_name"], "Fábrica Norte")
            with self.assertRaises(ValueError):
                self.call("chief", "create_inspection", {
                    "inspector_id": self.users["inspector"]["id"], "entity_name": "No existe",
                })

    def test_entity_can_be_changed_later_and_is_recorded(self):
        with self.entities():
            created = self.call("secretary", "create_inspection", {"company_name": "Fábrica"})
            inspection_id = created.response[1]["id"]
            denied = self.call("inspector", "act_on_inspection", {
                "inspection_id": inspection_id, "version": 0, "action": "set_entity",
                "entity_name": "Fábrica Norte",
            })
            self.assertEqual(denied.response[0], 403)
            changed = self.call("secretary", "act_on_inspection", {
                "inspection_id": inspection_id, "version": 0, "action": "set_entity",
                "entity_name": "Fábrica Norte",
            })
            self.assertEqual(changed.response[0], 200)
        row = self.inspection(inspection_id)
        self.assertEqual((row["entity_name"], row["stage"], row["version"]), ("Fábrica Norte", "assignment", 1))
        listing = self.call("secretary", "get_inspections")
        events = listing.response[1]["inspections"][0]["events"]
        self.assertIn("Entidad: (vacío) → Fábrica Norte", events[-1]["details_text"])

    def test_planning_editor_saves_entity(self):
        item = self.items()[("Fábrica", "Medidores")]
        with self.entities():
            handler = self.call("chief", "update_planning_item", {
                "id": item["id"], "version": item["version"], "entity_name": "Fábrica Norte",
            })
        row = {entry["id"]: entry for entry in handler.response[1]["items"]}[item["id"]]
        self.assertEqual(row["entity_name"], "Fábrica Norte")
        self.assertEqual(row["edited"], [])
        synced = self.call("chief", "sync_inspection_planning", {})
        row = {entry["id"]: entry for entry in synced.response[1]["items"]}[item["id"]]
        self.assertEqual(row["entity_name"], "Fábrica Norte")

    # --- Correcciones de la revisión de código ---

    def rewrite_industrial(self, next_fabrica):
        """Vuelve a escribir la planilla industrial con otra próxima fecha para Fábrica."""
        write(self.root / "INSPECCIONES" / "PL-11-16 Planilla de instalaciones INDUSTRIALES.xlsx", {
            "MEDIDORES": [
                [], [],
                ["USUARIO:", "DIRECCIÓN", "DEPARTAMENTO:", None, "ÚLTIMA  INSPECCIÓN:", "PRÓXIMA INSPECCIÓN:"],
                [None, "DE INSTALACIÓN:", None, "FECHA:", "C/OBS:", None],
                ["Fábrica", "Ruta 8", "LAVALLEJA", datetime(2025, 11, 1), None, next_fabrica],
                ["Puerto", "Rambla", "MONTEVIDEO", datetime(2020, 1, 1), None, datetime(2022, 1, 1)],
            ],
        })
        inspection_planning._cache.clear()

    def assert_unlocked(self, real):
        """Envuelve la validación de entidad para comprobar que corre sin la base bloqueada."""
        calls = []

        def check(value):
            other = sqlite3.connect(database.DATABASE, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()
            calls.append(value)
            return real(value)
        return check, calls

    def test_rereading_changed_sheets_blocks_a_stale_editor(self):
        item = self.items()[("Fábrica", "Medidores")]
        self.rewrite_industrial(datetime(2028, 2, 1))
        self.call("chief", "sync_inspection_planning", {})
        fresh = self.items()[("Fábrica", "Medidores")]
        self.assertEqual(fresh["next_inspection"], "2028-02-01")
        self.assertEqual(fresh["version"], item["version"] + 1)
        # El editor abierto antes de releer no fija la fecha vieja como corrección manual.
        stale = self.call("chief", "update_planning_item", {
            "id": item["id"], "version": item["version"],
            "next_inspection": item["next_inspection"], "notes": "x",
        })
        self.assertEqual(stale.response[0], 409)
        self.assertEqual(self.items()[("Fábrica", "Medidores")]["edited"], [])
        # Releer sin cambios no sube la versión.
        self.call("chief", "sync_inspection_planning", {})
        self.assertEqual(self.items()[("Fábrica", "Medidores")]["version"], fresh["version"])

    def test_manual_value_matching_the_sheet_stops_being_manual(self):
        item = self.items()[("Fábrica", "Medidores")]
        edited = self.call("chief", "update_planning_item", {
            "id": item["id"], "version": item["version"], "next_inspection": "2027-03-01",
        })
        self.assertEqual(edited.response[0], 200)
        self.assertEqual(self.items()[("Fábrica", "Medidores")]["edited"], ["next_inspection"])
        self.rewrite_industrial(datetime(2027, 3, 1))
        self.call("chief", "sync_inspection_planning", {})
        row = self.items()[("Fábrica", "Medidores")]
        self.assertEqual((row["edited"], row["next_inspection"]), ([], "2027-03-01"))
        self.assertEqual(row["events"][-1]["action"], "La planilla ahora coincide con la corrección")
        self.assertIn("Próxima inspección", row["events"][-1]["details_text"])

    def test_row_with_renamed_entity_folder_stays_editable(self):
        item = self.items()[("Fábrica", "Medidores")]
        import routes.planning as planning_module
        check, calls = self.assert_unlocked(planning_module.entity_name_or_error)
        with self.entities(), mock.patch.object(planning_module, "entity_name_or_error", side_effect=check):
            linked = self.call("chief", "update_planning_item", {
                "id": item["id"], "version": item["version"], "entity_name": "fábrica norte",
            })
        self.assertEqual(calls, ["fábrica norte"])
        row = {entry["id"]: entry for entry in linked.response[1]["items"]}[item["id"]]
        # La carpeta de la entidad se renombró: ya no figura entre las entidades.
        with mock.patch("routes.inspections._entity_areas", return_value=([], 0)):
            saved = self.call("chief", "update_planning_item", {
                "id": item["id"], "version": row["version"], "entity_name": "Fábrica Norte",
                "notes": "Sigue siendo editable",
            })
            self.assertEqual(saved.response[0], 200)
            row = {entry["id"]: entry for entry in saved.response[1]["items"]}[item["id"]]
            self.assertEqual((row["notes"], row["entity_name"]), ("Sigue siendo editable", "Fábrica Norte"))
            with self.assertRaises(ValueError):
                self.call("chief", "update_planning_item", {
                    "id": item["id"], "version": row["version"], "entity_name": "Otra",
                })

    def test_scheduling_links_entity_and_bumps_planning_version(self):
        item = self.items()[("Fábrica", "Medidores")]
        with self.entities():
            created = self.call("chief", "create_inspection", {
                "company_name": "Fábrica", "entity_name": "Fábrica Norte",
                "inspector_id": self.users["inspector"]["id"], "planning_item_id": item["id"],
            })
            self.assertEqual(created.response[0], 201)
            row = self.items()[("Fábrica", "Medidores")]
            self.assertEqual((row["entity_name"], row["version"]), ("Fábrica Norte", item["version"] + 1))
            self.assertIn("Entidad: (vacío) → Fábrica Norte", row["events"][-1]["details_text"])
            # Un editor abierto antes de agendar ya no borra la entidad al guardar.
            stale = self.call("chief", "update_planning_item", {
                "id": item["id"], "version": item["version"], "entity_name": "", "notes": "x",
            })
            self.assertEqual(stale.response[0], 409)
        # El departamento de la planilla va a Departamento de la ficha, no a Ciudad.
        connection = database.connect_db()
        profile = connection.execute(
            "SELECT city, department FROM entity_profiles WHERE entity_name = 'Fábrica Norte'"
        ).fetchone()
        connection.close()
        self.assertEqual((profile["city"], profile["department"]), ("", "LAVALLEJA"))

    def test_set_entity_checks_outside_lock_and_bumps_planning_version(self):
        item = self.items()[("Puerto", "Medidores")]
        created = self.call("chief", "create_inspection", {
            "inspector_id": self.users["inspector"]["id"], "planning_item_id": item["id"],
        })
        inspection_id = created.response[1]["id"]
        import routes.inspections as inspections_module
        check, calls = self.assert_unlocked(inspections_module.entity_name_or_error)
        with self.entities(), mock.patch.object(inspections_module, "entity_name_or_error", side_effect=check):
            changed = self.call("chief", "act_on_inspection", {
                "inspection_id": inspection_id, "version": 1, "action": "set_entity",
                "entity_name": "Fábrica Norte",
            })
        self.assertEqual((changed.response[0], calls), (200, ["Fábrica Norte"]))
        row = self.items()[("Puerto", "Medidores")]
        self.assertEqual((row["entity_name"], row["version"]), ("Fábrica Norte", item["version"] + 1))
        self.assertIn(f"Inspección: #{inspection_id}", row["events"][-1]["details_text"])

    def test_second_inspector_needs_a_lead_first(self):
        created = self.call("secretary", "create_inspection", {"company_name": "Fábrica"})
        with self.assertRaises(ValueError):
            self.call("chief", "act_on_inspection", {
                "inspection_id": created.response[1]["id"], "version": 0, "action": "set_second",
                "second_inspector_id": self.users["inspector"]["id"],
            })
        self.assertIsNone(self.inspection(created.response[1]["id"])["second_inspector_id"])

    def export(self, user, **params):
        handler = Handler(self.users[user])
        handler.export_inspection_planning({key: [value] for key, value in params.items()})
        return handler

    def test_export_respects_filters_and_stores_text(self):
        handler = self.export("chief", status="vencida", category="Industriales", sort="institution")
        self.assertEqual(handler.response[0], 200)
        self.assertIn("planificacion_inspecciones.xlsx", handler.headers_sent["Content-Disposition"])
        workbook = load_workbook(BytesIO(handler.wfile.getvalue()))
        rows = list(workbook["Planificación"].iter_rows(min_row=2))
        self.assertEqual([row[0].value for row in rows], ["@SUMA(A1)", "Puerto"])
        for row in rows:
            for cell in row:
                if cell.value is not None:
                    self.assertEqual(cell.data_type, "s")
        self.assertEqual(rows[1][4].value, "01/01/2022")
        self.assertEqual(rows[1][5].value, "Vencida")
        filters = {row[0]: row[1] for row in workbook["Filtros"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(filters["Estado"], "Vencida")
        self.assertEqual(filters["Planilla"], "Industriales")

    def test_export_by_month_search_and_invalid_values(self):
        sheet = load_workbook(BytesIO(self.export("admin", month="2026-12").wfile.getvalue()))
        names = {row[0] for row in sheet["Planificación"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(names, {"Fábrica"})
        filters = {row[0]: row[1] for row in sheet["Filtros"].iter_rows(min_row=2, values_only=True)}
        self.assertEqual(filters["Vencen en"], "diciembre de 2026")
        handler = self.export("admin", q="hospital uno", status="cualquiera", sort="otro", month="2026-13")
        sheet = load_workbook(BytesIO(handler.wfile.getvalue()))["Planificación"]
        self.assertEqual({row[0] for row in sheet.iter_rows(min_row=2, values_only=True)}, {"Hospital Uno"})
        self.assertEqual(self.export("inspector").response[0], 403)

if __name__ == "__main__":
    unittest.main()
