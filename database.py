import json
import sqlite3
from datetime import datetime, timezone

from settings import DATABASE, INDEX_STATE, INDEX_STATE_LOCK

ENTITY_WORKFLOW_NAMES = (
    "Licencia de Operación",
    "Licencia de Construcción",
    "Comercialización y servicio",
    "Autorización de transporte",
)


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    # Con WAL, NORMAL evita sincronizar el disco en cada commit sin arriesgar
    # la integridad de la base (solo puede perderse el último commit ante un corte de luz).
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _create_inspections_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS inspections ("
        "id INTEGER PRIMARY KEY, inspector_id INTEGER REFERENCES workflow_users(id), "
        "created_by INTEGER NOT NULL REFERENCES workflow_users(id), created_at TEXT NOT NULL, "
        "application_date TEXT NOT NULL DEFAULT '', case_number TEXT NOT NULL DEFAULT '', "
        "company_name TEXT NOT NULL DEFAULT '', rut TEXT NOT NULL DEFAULT '', "
        "request_type TEXT NOT NULL DEFAULT '', inspection_type TEXT NOT NULL DEFAULT '', "
        "practice TEXT NOT NULL DEFAULT '', equipment_model TEXT NOT NULL DEFAULT '', "
        "contact_details TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', "
        "address TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', "
        "stage TEXT NOT NULL DEFAULT 'assignment', version INTEGER NOT NULL DEFAULT 0, "
        "scheduled_date TEXT NOT NULL DEFAULT '', scheduled_time TEXT NOT NULL DEFAULT '', "
        "scheduled_place TEXT NOT NULL DEFAULT '', pre_checklist TEXT NOT NULL DEFAULT '[]', "
        "post_checklist TEXT NOT NULL DEFAULT '[]')"
    )


def _initialize_inspections(connection: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(inspections)")}
    if columns and columns["inspector_id"]["notnull"]:
        connection.execute("SAVEPOINT migrate_inspections")
        try:
            connection.execute("ALTER TABLE inspections RENAME TO inspections_legacy")
            _create_inspections_table(connection)
            fields = (
                "id, inspector_id, created_by, created_at, application_date, case_number, "
                "company_name, rut, request_type, inspection_type, practice, equipment_model, "
                "contact_details, phone, address, city, email"
            )
            connection.execute(
                f"INSERT INTO inspections ({fields}, stage) "
                f"SELECT {fields}, 'pre_inspection' FROM inspections_legacy"
            )
            connection.execute("DROP TABLE inspections_legacy")
            connection.execute("RELEASE SAVEPOINT migrate_inspections")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT migrate_inspections")
            connection.execute("RELEASE SAVEPOINT migrate_inspections")
            raise
    else:
        _create_inspections_table(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspections_inspector ON inspections(inspector_id)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS inspection_events ("
        "id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL REFERENCES inspections(id), "
        "actor_id INTEGER NOT NULL REFERENCES workflow_users(id), "
        "action TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
    )
    # Requerimientos que el inspector agrega en el checklist posterior, con su plazo.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS inspection_requirements ("
        "id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL REFERENCES inspections(id), "
        "text TEXT NOT NULL, due_date TEXT NOT NULL, "
        "created_by INTEGER NOT NULL REFERENCES workflow_users(id), created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspection_requirements_inspection "
        "ON inspection_requirements(inspection_id)"
    )
    requirement_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(inspection_requirements)")
    }
    if "resolved_at" not in requirement_columns:
        connection.execute(
            "ALTER TABLE inspection_requirements ADD COLUMN resolved_at TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE inspection_requirements ADD COLUMN resolved_by INTEGER "
            "REFERENCES workflow_users(id)"
        )
    # Historial detallado (se agregan columnas sin tocar los registros existentes).
    event_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(inspection_events)")
    }
    for column, definition in (
        ("from_stage", "TEXT NOT NULL DEFAULT ''"),
        ("to_stage", "TEXT NOT NULL DEFAULT ''"),
        ("details", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if column not in event_columns:
            connection.execute(f"ALTER TABLE inspection_events ADD COLUMN {column} {definition}")
    inspection_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(inspections)")
    }
    if "updated_at" not in inspection_columns:
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
        )
    if "second_inspector_id" not in inspection_columns:
        # Segundo inspector: solo ve la inspección agendada; no interviene en el flujo.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN second_inspector_id INTEGER "
            "REFERENCES workflow_users(id)"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspections_second_inspector "
        "ON inspections(second_inspector_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspection_events_inspection "
        "ON inspection_events(inspection_id, id)"
    )
    _initialize_planning(connection, inspection_columns)


def _initialize_planning(connection: sqlite3.Connection, inspection_columns) -> None:
    """Planificación de inspección: copia de las planillas de la carpeta 5.

    Los valores leídos de las planillas (sheet_*) se actualizan al volver a leerlas;
    las correcciones manuales (manual_*) y las notas nunca se pisan.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS planning_items ("
        "id INTEGER PRIMARY KEY, item_key TEXT NOT NULL UNIQUE, "
        "sheet_institution TEXT NOT NULL DEFAULT '', sheet_practice TEXT NOT NULL DEFAULT '', "
        "sheet_department TEXT NOT NULL DEFAULT '', sheet_last_inspection TEXT NOT NULL DEFAULT '', "
        "sheet_next_inspection TEXT NOT NULL DEFAULT '', "
        "sheet_equipment_count INTEGER NOT NULL DEFAULT 0, "
        "categories TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]', "
        "in_sheets INTEGER NOT NULL DEFAULT 1, "
        "manual_institution TEXT, manual_practice TEXT, manual_department TEXT, "
        "manual_last_inspection TEXT, manual_next_inspection TEXT, "
        "notes TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL DEFAULT '', sheet_synced_at TEXT NOT NULL DEFAULT '', "
        "updated_at TEXT NOT NULL DEFAULT '', updated_by INTEGER REFERENCES workflow_users(id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS planning_events ("
        "id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES planning_items(id), "
        "actor_id INTEGER NOT NULL REFERENCES workflow_users(id), action TEXT NOT NULL, "
        "created_at TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}')"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_planning_events_item ON planning_events(item_id, id)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS planning_sync ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), synced_at TEXT NOT NULL, "
        "synced_by INTEGER REFERENCES workflow_users(id), "
        "files TEXT NOT NULL DEFAULT '[]', errors TEXT NOT NULL DEFAULT '[]', "
        "root TEXT NOT NULL DEFAULT '', root_exists INTEGER NOT NULL DEFAULT 0)"
    )
    sync_columns = {row["name"] for row in connection.execute("PRAGMA table_info(planning_sync)")}
    if "parser_version" not in sync_columns:
        connection.execute(
            "ALTER TABLE planning_sync ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 0"
        )
    planning_columns = {row["name"] for row in connection.execute("PRAGMA table_info(planning_items)")}
    if "entity_name" not in planning_columns:
        # Entidad usuaria (carpeta de "3 Entidades Usuarias") asociada a la fila.
        connection.execute(
            "ALTER TABLE planning_items ADD COLUMN entity_name TEXT NOT NULL DEFAULT ''"
        )
    if "entity_name" not in inspection_columns:
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN entity_name TEXT NOT NULL DEFAULT ''"
        )
    if "locomotion" not in inspection_columns:
        # Medio de locomoción elegido en el checklist previo.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN locomotion TEXT NOT NULL DEFAULT ''"
        )
    if "driver_status" not in inspection_columns:
        # Coordinación del chofer cuando se viaja en Auto MIEM: '', 'pending' o 'coordinated'.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN driver_status TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN driver_name TEXT NOT NULL DEFAULT ''"
        )
    # Los checklists cambiaron de 6 casillas genéricas a 3 ítems con nombre. En las etapas
    # donde todavía se completan, las 6 casillas viejas se vacían para no mostrarlas con
    # nombres que no les corresponden; en las demás se conservan (se ven como "Ítem N").
    for column, stage in (("pre_checklist", "pre_inspection"), ("post_checklist", "post_inspection")):
        for row in connection.execute(
            f"SELECT id, {column} AS value FROM inspections WHERE stage = ?", (stage,),
        ).fetchall():
            try:
                legacy = len(json.loads(row["value"] or "[]")) == 6
            except ValueError:
                legacy = True
            if legacy:
                connection.execute(f"UPDATE inspections SET {column} = '[]' WHERE id = ?", (row["id"],))
    if "send_mail_saved" not in inspection_columns:
        # Checklist de envío: el mail de envío quedó guardado en la línea de tiempo.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN send_mail_saved INTEGER NOT NULL DEFAULT 0"
        )
    if "next_inspection_date" not in inspection_columns:
        # Fecha de la próxima inspección, cargada en el checklist de envío.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN next_inspection_date TEXT NOT NULL DEFAULT ''"
        )
    if "aris_not_applicable" not in inspection_columns:
        # Checklist posterior: el ingreso en ARIS solo corresponde a equipos nuevos.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN aris_not_applicable INTEGER NOT NULL DEFAULT 0"
        )
    if "departure_time" not in inspection_columns:
        # Horarios del viaje en Auto MIEM, para coordinar el chofer.
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN departure_time TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN arrival_time TEXT NOT NULL DEFAULT ''"
        )
    # Ficha de cada entidad usuaria: datos que se van juntando (manual o desde inspecciones).
    connection.execute(
        "CREATE TABLE IF NOT EXISTS entity_profiles ("
        "id INTEGER PRIMARY KEY, entity_key TEXT NOT NULL UNIQUE, entity_name TEXT NOT NULL, "
        "rut TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', "
        "city TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '', "
        "phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', "
        "contact_details TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', "
        "version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT '', updated_by INTEGER REFERENCES workflow_users(id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS entity_profile_events ("
        "id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES entity_profiles(id), "
        "actor_id INTEGER NOT NULL REFERENCES workflow_users(id), action TEXT NOT NULL, "
        "created_at TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}')"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_profile_events ON entity_profile_events(profile_id, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_planning_items_entity ON planning_items(entity_name)"
    )
    if "planning_item_id" not in inspection_columns:
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN planning_item_id INTEGER "
            "REFERENCES planning_items(id)"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inspections_planning_item "
        "ON inspections(planning_item_id)"
    )


def initialize_db() -> None:
    connection = connect_db()
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS file_catalog ("
            "root TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, "
            "extension TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', "
            "size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, "
            "created_ns INTEGER NOT NULL DEFAULT 0, "
            "is_authorization_certificate INTEGER NOT NULL DEFAULT 0, "
            "scan_id TEXT NOT NULL, PRIMARY KEY(root, path))"
        )
        catalog_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(file_catalog)")
        }
        if "created_ns" not in catalog_columns:
            connection.execute(
                "ALTER TABLE file_catalog ADD COLUMN created_ns INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_catalog_owner "
            "ON file_catalog(root, owner, path)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_catalog_extension "
            "ON file_catalog(root, extension, path)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS file_catalog_state ("
            "root TEXT PRIMARY KEY, last_success TEXT NOT NULL, "
            "file_count INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS extracted_records ("
            "id INTEGER PRIMARY KEY, source_path TEXT NOT NULL UNIQUE, "
            "created_at TEXT NOT NULL, first_name TEXT NOT NULL DEFAULT '', "
            "last_name TEXT NOT NULL DEFAULT '', identity_number TEXT NOT NULL DEFAULT '', "
            "authorization_number TEXT NOT NULL DEFAULT '', practice TEXT NOT NULL DEFAULT '', "
            "issue_date TEXT NOT NULL DEFAULT '', expiry_date TEXT NOT NULL DEFAULT '', "
            "equipment TEXT NOT NULL DEFAULT '', "
            "is_authorization_certificate INTEGER NOT NULL DEFAULT 0, "
            "needs_review INTEGER NOT NULL DEFAULT 1)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS certificate_import_cache ("
            "root TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, "
            "modified_ns INTEGER NOT NULL, parser_version INTEGER NOT NULL, "
            "outcome TEXT NOT NULL CHECK(outcome IN ('certificate', 'other', 'without_text')), "
            "PRIMARY KEY(root, path))"
        )
        # Licencias de operación ya revisadas, por tamaño y fecha exactos: la revisión
        # automática no vuelve a leer las que no cambiaron (ni las escaneadas sin texto).
        connection.execute(
            "CREATE TABLE IF NOT EXISTS entity_license_import_cache ("
            "path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, "
            "outcome TEXT NOT NULL CHECK(outcome IN ('license', 'without_text', 'without_license')))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS entity_licenses ("
            "source_path TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "license_number TEXT NOT NULL, representative TEXT NOT NULL DEFAULT '', "
            "authorization_number TEXT NOT NULL DEFAULT '', "
            "issue_date TEXT NOT NULL DEFAULT '', expiry_date TEXT NOT NULL DEFAULT '', "
            "technical_tables TEXT)"
        )
        license_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(entity_licenses)")
        }
        for column in ("representative", "authorization_number", "issue_date", "expiry_date"):
            if column not in license_columns:
                connection.execute(
                    f"ALTER TABLE entity_licenses ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        if "technical_tables" not in license_columns:
            connection.execute("ALTER TABLE entity_licenses ADD COLUMN technical_tables TEXT")
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(extracted_records)")
        }
        for column in ("practice", "issue_date", "expiry_date"):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE extracted_records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        if "is_authorization_certificate" not in columns:
            connection.execute(
                "ALTER TABLE extracted_records ADD COLUMN "
                "is_authorization_certificate INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_users ("
            "id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, "
            "display_name TEXT NOT NULL, password_hash TEXT NOT NULL, "
            "role TEXT NOT NULL CHECK(role IN ('admin', 'user')), "
            "workflow_role TEXT NOT NULL DEFAULT '', "
            "active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"
        )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(workflow_users)")
        }
        if "workflow_role" not in user_columns:
            connection.execute(
                "ALTER TABLE workflow_users ADD COLUMN workflow_role TEXT NOT NULL DEFAULT ''"
            )
        _initialize_inspections(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_sessions ("
            "token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
            "expires_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_groups ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, "
            "created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_group_members ("
            "group_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "PRIMARY KEY(group_id, user_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_definitions ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, "
            "description TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, "
            "active INTEGER NOT NULL DEFAULT 1, created_by INTEGER, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_stages ("
            "id INTEGER PRIMARY KEY, workflow_id INTEGER NOT NULL, stage_key TEXT NOT NULL, "
            "name TEXT NOT NULL, position INTEGER NOT NULL, "
            "default_assignee_user_id INTEGER, default_assignee_group_id INTEGER, "
            "UNIQUE(workflow_id, stage_key), UNIQUE(workflow_id, position))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_transitions ("
            "id INTEGER PRIMARY KEY, workflow_id INTEGER NOT NULL, "
            "from_stage_id INTEGER NOT NULL, to_stage_id INTEGER NOT NULL, "
            "label TEXT NOT NULL, UNIQUE(from_stage_id, to_stage_id, label))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_cases ("
            "id INTEGER PRIMARY KEY, workflow_id INTEGER NOT NULL, "
            "workflow_version INTEGER NOT NULL, title TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '', "
            "description TEXT NOT NULL DEFAULT '', request_type TEXT NOT NULL DEFAULT '', "
            "linked_user_name TEXT NOT NULL DEFAULT '', "
            "linked_document_path TEXT NOT NULL DEFAULT '', current_stage_id INTEGER NOT NULL, "
            "assigned_user_id INTEGER, assigned_group_id INTEGER, claimed_by_user_id INTEGER, "
            "status TEXT NOT NULL CHECK(status IN ('active', 'completed')), "
            "created_by INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "entered_stage_at TEXT NOT NULL, completed_at TEXT, delivered_at TEXT, delivered_by INTEGER, "
            "delivery_method TEXT)"
        )
        case_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(workflow_cases)")
        }
        if "request_type" not in case_columns:
            connection.execute(
                "ALTER TABLE workflow_cases ADD COLUMN request_type TEXT NOT NULL DEFAULT ''"
            )
        if "delivered_at" not in case_columns:
            connection.execute("ALTER TABLE workflow_cases ADD COLUMN delivered_at TEXT")
        if "delivered_by" not in case_columns:
            connection.execute("ALTER TABLE workflow_cases ADD COLUMN delivered_by INTEGER")
        if "delivery_method" not in case_columns:
            connection.execute("ALTER TABLE workflow_cases ADD COLUMN delivery_method TEXT")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_case_application_data ("
            "case_id INTEGER PRIMARY KEY REFERENCES workflow_cases(id) ON DELETE CASCADE, "
            "application_date TEXT NOT NULL, form_number TEXT NOT NULL, short_code TEXT NOT NULL, "
            "first_name TEXT NOT NULL, last_name TEXT NOT NULL, identity_number TEXT NOT NULL, "
            "profession TEXT NOT NULL, practice TEXT NOT NULL, workplace TEXT NOT NULL, "
            "email TEXT NOT NULL, assigned_code TEXT NOT NULL, "
            "digital_signature INTEGER NOT NULL CHECK(digital_signature IN (0, 1)))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_entity_types ("
            "workflow_id INTEGER PRIMARY KEY REFERENCES workflow_definitions(id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_case_entity_data ("
            "case_id INTEGER PRIMARY KEY REFERENCES workflow_cases(id) ON DELETE CASCADE, "
            "application_date TEXT NOT NULL, form_number TEXT NOT NULL, "
            "company_name TEXT NOT NULL, practice TEXT NOT NULL, code TEXT NOT NULL, "
            "digital_signature INTEGER NOT NULL CHECK(digital_signature IN (0, 1)), "
            "linked_entity_name TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_case_stage_assignments ("
            "case_id INTEGER NOT NULL, stage_id INTEGER NOT NULL, "
            "user_id INTEGER, group_id INTEGER, PRIMARY KEY(case_id, stage_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_events ("
            "id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, "
            "event_type TEXT NOT NULL, from_stage_id INTEGER, to_stage_id INTEGER, "
            "from_assigned_user_id INTEGER, from_assigned_group_id INTEGER, "
            "to_assigned_user_id INTEGER, to_assigned_group_id INTEGER, "
            "comment TEXT NOT NULL DEFAULT '', duration_seconds INTEGER, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_cases_status "
            "ON workflow_cases(status, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_events_case "
            "ON workflow_events(case_id, id)"
        )
        from auth import bootstrap_admin

        bootstrap_admin(connection)
        _seed_authorization_workflow(connection)
        _ensure_authorization_returns(connection)
        _ensure_inspector_assignment_stage(connection)
        _ensure_return_to_assigned_inspector(connection)
        _ensure_inspection_rejection(connection)
        _seed_entity_workflows(connection)
        catalog_ready = connection.execute(
            "SELECT 1 FROM file_catalog_state WHERE root = 'documents'"
        ).fetchone() is not None
        with INDEX_STATE_LOCK:
            INDEX_STATE["catalog_ready"] = catalog_ready
        connection.commit()
    finally:
        connection.close()


def _seed_authorization_workflow(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM workflow_definitions LIMIT 1"
    ).fetchone():
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    admin = connection.execute(
        "SELECT id FROM workflow_users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    cursor = connection.execute(
        "INSERT INTO workflow_definitions "
        "(name, description, version, active, created_by, created_at) "
        "VALUES (?, ?, 1, 1, ?, ?)",
        (
            "Autorización Individual",
            "Flujo inicial para tramitar una autorización individual.",
            admin["id"] if admin else None,
            now,
        ),
    )
    workflow_id = cursor.lastrowid
    stages = [
        ("carga_datos", "Carga de datos"),
        ("evaluacion_inspector", "Evaluación del inspector"),
        ("jefe_licenciamiento", "Jefe de licenciamiento"),
        ("abogado", "Abogado"),
        ("director", "Director"),
        ("finalizado", "Finalizado"),
    ]
    stage_ids = {}
    for position, (stage_key, name) in enumerate(stages, start=1):
        cursor = connection.execute(
            "INSERT INTO workflow_stages (workflow_id, stage_key, name, position) "
            "VALUES (?, ?, ?, ?)",
            (workflow_id, stage_key, name, position),
        )
        stage_ids[stage_key] = cursor.lastrowid
    transitions = [
        ("carga_datos", "evaluacion_inspector", "Enviar a evaluación del inspector"),
        ("evaluacion_inspector", "carga_datos", "Observado: devolver a carga de datos"),
        ("evaluacion_inspector", "jefe_licenciamiento", "Aprobado: enviar al jefe de licenciamiento"),
        ("jefe_licenciamiento", "evaluacion_inspector", "Devolver a evaluación del inspector"),
        ("jefe_licenciamiento", "abogado", "Enviar a abogado"),
        ("abogado", "director", "Enviar a director"),
        ("director", "finalizado", "Finalizar"),
    ]
    for source, target, label in transitions:
        connection.execute(
            "INSERT INTO workflow_transitions "
            "(workflow_id, from_stage_id, to_stage_id, label) VALUES (?, ?, ?, ?)",
            (workflow_id, stage_ids[source], stage_ids[target], label),
        )


def _seed_entity_workflows(connection: sqlite3.Connection) -> None:
    source = connection.execute(
        "SELECT id FROM workflow_definitions WHERE name = ?", ("Autorización Individual",),
    ).fetchone()
    if not source:
        return
    defaults = {
        row["stage_key"]: (row["default_assignee_user_id"], row["default_assignee_group_id"])
        for row in connection.execute(
            "SELECT stage_key, default_assignee_user_id, default_assignee_group_id "
            "FROM workflow_stages WHERE workflow_id = ?", (source["id"],),
        )
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    admin = connection.execute(
        "SELECT id FROM workflow_users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    for name in ENTITY_WORKFLOW_NAMES:
        existing = connection.execute(
            "SELECT definitions.id, entity.workflow_id AS managed "
            "FROM workflow_definitions AS definitions LEFT JOIN workflow_entity_types AS entity "
            "ON entity.workflow_id = definitions.id WHERE definitions.name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if existing:
            if existing["managed"] is None:
                raise ValueError(f"Ya existe un flujo con el nombre {name}; no se modificó.")
            continue
        workflow_id = connection.execute(
            "INSERT INTO workflow_definitions "
            "(name, description, version, active, created_by, created_at) VALUES (?, ?, 1, 1, ?, ?)",
            (name, "Trámite de entidad con asignación directa a Inspección.",
             admin["id"] if admin else None, now),
        ).lastrowid
        connection.execute("INSERT INTO workflow_entity_types (workflow_id) VALUES (?)", (workflow_id,))
        stage_ids = {}
        for position, (key, label) in enumerate((
            ("carga_datos", "Carga de datos"),
            ("evaluacion_inspector", "Evaluación del inspector"),
            ("jefe_licenciamiento", "Jefe de licenciamiento"),
            ("abogado", "Abogado"), ("director", "Director"),
            ("finalizado", "Finalizado"), ("rechazado", "Rechazado"),
        ), start=1):
            user_id, group_id = defaults.get(key, (None, None))
            stage_ids[key] = connection.execute(
                "INSERT INTO workflow_stages (workflow_id, stage_key, name, position, "
                "default_assignee_user_id, default_assignee_group_id) VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, key, label, position, user_id, group_id),
            ).lastrowid
        for source_key, target_key, label in (
            ("carga_datos", "evaluacion_inspector", "Enviar al funcionario de Inspección"),
            ("evaluacion_inspector", "carga_datos", "Observado: devolver a Secretaría"),
            ("evaluacion_inspector", "jefe_licenciamiento", "Aprobado: enviar al jefe de licenciamiento"),
            ("evaluacion_inspector", "rechazado", "Rechazado: finalizar trámite"),
            ("jefe_licenciamiento", "evaluacion_inspector", "Devolver a evaluación del inspector"),
            ("jefe_licenciamiento", "abogado", "Enviar a abogado"),
            ("abogado", "jefe_licenciamiento", "Devolver a Jefe de Licenciamiento"),
            ("abogado", "director", "Enviar a director"),
            ("director", "evaluacion_inspector", "Devolver a Inspección"),
            ("director", "jefe_licenciamiento", "Devolver a Jefe de Licenciamiento"),
            ("director", "finalizado", "Finalizar"),
        ):
            connection.execute(
                "INSERT INTO workflow_transitions (workflow_id, from_stage_id, to_stage_id, label) "
                "VALUES (?, ?, ?, ?)", (workflow_id, stage_ids[source_key], stage_ids[target_key], label),
            )


def _ensure_inspection_rejection(connection: sqlite3.Connection) -> None:
    workflow = connection.execute(
        "SELECT id FROM workflow_definitions WHERE name = ?", ("Autorización Individual",),
    ).fetchone()
    if not workflow:
        return
    stages = {
        row["stage_key"]: row["id"]
        for row in connection.execute(
            "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?", (workflow["id"],),
        )
    }
    if "evaluacion_inspector" not in stages:
        raise ValueError("El flujo de Autorización Individual no tiene etapa de Inspección.")
    changed = False
    if "rechazado" not in stages:
        position = connection.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM workflow_stages WHERE workflow_id = ?",
            (workflow["id"],),
        ).fetchone()[0]
        stages["rechazado"] = connection.execute(
            "INSERT INTO workflow_stages (workflow_id, stage_key, name, position) VALUES (?, ?, ?, ?)",
            (workflow["id"], "rechazado", "Rechazado", position),
        ).lastrowid
        changed = True
    if not connection.execute(
        "SELECT 1 FROM workflow_transitions WHERE workflow_id = ? AND from_stage_id = ? AND to_stage_id = ?",
        (workflow["id"], stages["evaluacion_inspector"], stages["rechazado"]),
    ).fetchone():
        connection.execute(
            "INSERT INTO workflow_transitions (workflow_id, from_stage_id, to_stage_id, label) "
            "VALUES (?, ?, ?, ?)",
            (workflow["id"], stages["evaluacion_inspector"], stages["rechazado"],
             "Rechazado: finalizar trámite"),
        )
        changed = True
    if changed:
        connection.execute(
            "UPDATE workflow_definitions SET version = version + 1 WHERE id = ?", (workflow["id"],),
        )


def _ensure_return_to_assigned_inspector(connection: sqlite3.Connection) -> None:
    workflow = connection.execute(
        "SELECT id FROM workflow_definitions WHERE name = ?",
        ("Autorización Individual",),
    ).fetchone()
    if not workflow:
        return
    stages = {
        row["stage_key"]: row["id"]
        for row in connection.execute(
            "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?",
            (workflow["id"],),
        )
    }
    if not all(key in stages for key in ("carga_datos", "evaluacion_inspector")):
        return
    if not connection.execute(
        "SELECT 1 FROM workflow_transitions WHERE workflow_id = ? "
        "AND from_stage_id = ? AND to_stage_id = ?",
        (workflow["id"], stages["carga_datos"], stages["evaluacion_inspector"]),
    ).fetchone():
        connection.execute(
            "INSERT INTO workflow_transitions "
            "(workflow_id, from_stage_id, to_stage_id, label) VALUES (?, ?, ?, ?)",
            (workflow["id"], stages["carga_datos"], stages["evaluacion_inspector"],
             "Enviar al funcionario de Inspección"),
        )
        connection.execute(
            "UPDATE workflow_definitions SET version = version + 1 WHERE id = ?",
            (workflow["id"],),
        )


def _ensure_authorization_returns(connection: sqlite3.Connection) -> None:
    workflow = connection.execute(
        "SELECT id FROM workflow_definitions WHERE name = ?",
        ("Autorización Individual",),
    ).fetchone()
    if not workflow:
        return
    stages = {
        row["stage_key"]: row["id"]
        for row in connection.execute(
            "SELECT id, stage_key FROM workflow_stages WHERE workflow_id = ?",
            (workflow["id"],),
        )
    }
    changed = False
    if "abogado" in stages and "evaluacion_inspector" in stages:
        deleted = connection.execute(
            "DELETE FROM workflow_transitions WHERE workflow_id = ? "
            "AND from_stage_id = ? AND to_stage_id = ?",
            (workflow["id"], stages["abogado"], stages["evaluacion_inspector"]),
        )
        changed = deleted.rowcount > 0
    for source, target, label in (
        ("abogado", "jefe_licenciamiento", "Devolver a Jefe de Licenciamiento"),
        ("director", "evaluacion_inspector", "Devolver a Inspección"),
        ("director", "jefe_licenciamiento", "Devolver a Jefe de Licenciamiento"),
    ):
        if source not in stages or target not in stages:
            continue
        if not connection.execute(
            "SELECT 1 FROM workflow_transitions WHERE workflow_id = ? "
            "AND from_stage_id = ? AND to_stage_id = ?",
            (workflow["id"], stages[source], stages[target]),
        ).fetchone():
            connection.execute(
                "INSERT INTO workflow_transitions "
                "(workflow_id, from_stage_id, to_stage_id, label) VALUES (?, ?, ?, ?)",
                (workflow["id"], stages[source], stages[target], label),
            )
            changed = True
    if changed:
        connection.execute(
            "UPDATE workflow_definitions SET version = version + 1 WHERE id = ?",
            (workflow["id"],),
        )


def _ensure_inspector_assignment_stage(connection: sqlite3.Connection) -> None:
    workflow = connection.execute(
        "SELECT id FROM workflow_definitions WHERE name = ?",
        ("Autorización Individual",),
    ).fetchone()
    if not workflow:
        return
    stages = {
        row["stage_key"]: row
        for row in connection.execute(
            "SELECT id, stage_key, position, default_assignee_user_id, "
            "default_assignee_group_id FROM workflow_stages WHERE workflow_id = ?",
            (workflow["id"],),
        )
    }
    if "asignacion_inspector" in stages:
        return
    if not all(key in stages for key in ("carga_datos", "evaluacion_inspector", "jefe_licenciamiento")):
        raise ValueError("El flujo de Autorización Individual no tiene las etapas esperadas.")
    start = stages["carga_datos"]
    inspection = stages["evaluacion_inspector"]
    chief = stages["jefe_licenciamiento"]
    routes = connection.execute(
        "SELECT id FROM workflow_transitions WHERE workflow_id = ? "
        "AND from_stage_id = ? AND to_stage_id = ?",
        (workflow["id"], start["id"], inspection["id"]),
    ).fetchall()
    if len(routes) != 1:
        raise ValueError("No se pudo actualizar la transición inicial de Autorización Individual.")
    for stage in sorted(stages.values(), key=lambda row: row["position"], reverse=True):
        if stage["position"] >= inspection["position"]:
            connection.execute(
                "UPDATE workflow_stages SET position = ? WHERE id = ?",
                (stage["position"] + 1, stage["id"]),
            )
    assignment_id = connection.execute(
        "INSERT INTO workflow_stages "
        "(workflow_id, stage_key, name, position, default_assignee_user_id, default_assignee_group_id) "
        "VALUES (?, 'asignacion_inspector', ?, ?, ?, ?)",
        (workflow["id"], "Jefe de licenciamiento: asignación de funcionario",
         inspection["position"], chief["default_assignee_user_id"], chief["default_assignee_group_id"]),
    ).lastrowid
    connection.execute(
        "UPDATE workflow_transitions SET to_stage_id = ?, label = ? WHERE id = ?",
        (assignment_id, "Enviar al jefe para asignar funcionario", routes[0]["id"]),
    )
    connection.execute(
        "INSERT INTO workflow_transitions (workflow_id, from_stage_id, to_stage_id, label) "
        "VALUES (?, ?, ?, ?)",
        (workflow["id"], assignment_id, inspection["id"], "Asignar y enviar a Inspección"),
    )
    connection.execute(
        "INSERT INTO workflow_case_stage_assignments (case_id, stage_id, user_id, group_id) "
        "SELECT cases.id, ?, "
        "CASE WHEN prior.case_id IS NULL THEN ? ELSE prior.user_id END, "
        "CASE WHEN prior.case_id IS NULL THEN ? ELSE prior.group_id END "
        "FROM workflow_cases AS cases LEFT JOIN workflow_case_stage_assignments AS prior "
        "ON prior.case_id = cases.id AND prior.stage_id = ? "
        "WHERE cases.workflow_id = ? AND cases.status = 'active'",
        (assignment_id, chief["default_assignee_user_id"], chief["default_assignee_group_id"],
         chief["id"], workflow["id"]),
    )
    connection.execute(
        "UPDATE workflow_definitions SET version = version + 1 WHERE id = ?",
        (workflow["id"],),
    )
