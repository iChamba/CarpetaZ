"""Planificación de inspección guardada en la base.

Las planillas de la carpeta 5 son la fuente: al leerlas se actualizan las columnas
sheet_* de planning_items. Lo que Jefe de Inspección corrige se guarda en manual_*
y en las notas, que nunca se pisan al volver a leer las planillas. Los Excel nunca
se modifican.
"""

import json
import re
from datetime import date, datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import settings
from database import connect_db
from routes.entities import _entity_name_key
from routes.inspections import entity_name_or_error
from inspection_planning import (
    PARSER_VERSION, SORTS, STATUS_LABELS, filter_groups, item_key, load_planning, status_for,
)

EDITABLE = {
    "institution": 255, "practice": 160, "department": 160,
    "last_inspection": 10, "next_inspection": 10,
}
LABELS = {
    "institution": "Institución", "practice": "Práctica", "department": "Departamento",
    "last_inspection": "Última inspección", "next_inspection": "Próxima inspección",
    "notes": "Notas", "inspection": "Inspección", "entity_name": "Entidad",
    "fields": "Campos",
}
ACTION_LABELS = {
    "edit": "Datos corregidos", "restore": "Restaurados los datos de las planillas",
    "scheduled": "Inspección agendada",
    "sheet_match": "La planilla ahora coincide con la corrección",
    "inspection_next": "Próxima inspección actualizada desde el checklist de envío",
}
SORT_LABELS = {
    "next": "Próxima inspección", "last": "Última inspección",
    "institution": "Institución", "practice": "Práctica",
}
MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_chief(user):
    return user["role"] == "admin" or user.get("workflow_role") == "jefe_inspeccion"


def month_label(value: str) -> str:
    year, month = value.split("-")
    return f"{MONTHS[int(month) - 1]} de {year}"


def _valid_month(value: str) -> str:
    return value if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or "") else ""


def _day(value):
    return f"{value[8:10]}/{value[5:7]}/{value[0:4]}" if value else ""


def sync_planning(connection, root, user_id) -> dict:
    """Vuelca las planillas en planning_items sin tocar correcciones ni notas.

    Si la lectura cambia los datos de una fila, sube su versión: así un editor abierto
    con los datos viejos no los guarda como corrección manual. Una corrección manual
    que ahora coincide con la planilla deja de serlo.
    """
    planning = load_planning(root)
    now = _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = {
            row["item_key"]: row for row in connection.execute("SELECT * FROM planning_items")
        }
        seen = set()
        for group in planning["groups"]:
            key = item_key(group["institution"], group["practice"])
            seen.add(key)
            sheet = {field: group[field] or "" for field in EDITABLE}
            values = (
                *(sheet[field] for field in EDITABLE), group["equipment_count"],
                json.dumps(group["categories"], ensure_ascii=False),
                json.dumps(group["sources"], ensure_ascii=False), now,
            )
            row = existing.get(key)
            if row is None:
                connection.execute(
                    "INSERT INTO planning_items (sheet_institution, sheet_practice, "
                    "sheet_department, sheet_last_inspection, sheet_next_inspection, "
                    "sheet_equipment_count, categories, sources, sheet_synced_at, item_key, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, key, now),
                )
                continue
            changed = any(row[f"sheet_{field}"] != sheet[field] for field in EDITABLE)
            matched = [
                field for field in EDITABLE
                if row[f"manual_{field}"] is not None and row[f"manual_{field}"] == sheet[field]
            ]
            cleared = "".join(f", manual_{field} = NULL" for field in matched)
            connection.execute(
                "UPDATE planning_items SET sheet_institution = ?, sheet_practice = ?, "
                "sheet_department = ?, sheet_last_inspection = ?, sheet_next_inspection = ?, "
                "sheet_equipment_count = ?, categories = ?, sources = ?, sheet_synced_at = ?, "
                f"in_sheets = 1, version = version + ?{cleared} WHERE id = ?",
                (*values, 1 if changed or matched else 0, row["id"]),
            )
            if matched:
                connection.execute(
                    "INSERT INTO planning_events (item_id, actor_id, action, created_at, details) "
                    "VALUES (?, ?, 'sheet_match', ?, ?)",
                    (row["id"], user_id, now, json.dumps(
                        {"fields": ", ".join(LABELS[field] for field in matched)},
                        ensure_ascii=False,
                    )),
                )
        # Solo se marca "ya no figura" si todas las planillas se pudieron leer.
        if planning["root_exists"] and planning["files"] and not planning["errors"]:
            for (item_id, key) in connection.execute(
                "SELECT id, item_key FROM planning_items WHERE in_sheets = 1"
            ).fetchall():
                if key not in seen:
                    connection.execute(
                        "UPDATE planning_items SET in_sheets = 0 WHERE id = ?", (item_id,)
                    )
        connection.execute(
            "INSERT INTO planning_sync (id, synced_at, synced_by, files, errors, root, "
            "root_exists, parser_version) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "synced_at = excluded.synced_at, synced_by = excluded.synced_by, "
            "files = excluded.files, errors = excluded.errors, root = excluded.root, "
            "root_exists = excluded.root_exists, parser_version = excluded.parser_version",
            (now, user_id, json.dumps(planning["files"], ensure_ascii=False),
             json.dumps(planning["errors"], ensure_ascii=False), planning["root"],
             1 if planning["root_exists"] else 0, PARSER_VERSION),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return planning


def _effective(row, today: date) -> dict:
    item = {
        "id": row["id"], "version": row["version"], "notes": row["notes"],
        "entity_name": row["entity_name"],
        "in_sheets": bool(row["in_sheets"]),
        "equipment_count": row["sheet_equipment_count"],
        "categories": json.loads(row["categories"]), "sources": json.loads(row["sources"]),
        "updated_at": row["updated_at"], "updated_by_name": row["updated_by_name"] or "",
        "sheet": {}, "edited": [],
    }
    for field in EDITABLE:
        sheet_value = row[f"sheet_{field}"]
        manual = row[f"manual_{field}"]
        item["sheet"][field] = sheet_value
        item[field] = sheet_value if manual is None else manual
        if manual is not None:
            item["edited"].append(field)
    item["status"] = status_for(item["next_inspection"], today)
    return item


def load_items(connection, today: date | None = None) -> list:
    today = today or date.today()
    items = [
        _effective(row, today) for row in connection.execute(
            "SELECT planning_items.*, users.display_name AS updated_by_name "
            "FROM planning_items LEFT JOIN workflow_users AS users "
            "ON users.id = planning_items.updated_by"
        )
    ]
    by_id = {item["id"]: item for item in items}
    for item in items:
        item["inspections"] = []
        item["events"] = []
    for row in connection.execute(
        "SELECT id, planning_item_id, stage, scheduled_date FROM inspections "
        "WHERE planning_item_id IS NOT NULL ORDER BY id"
    ):
        if row["planning_item_id"] in by_id:
            by_id[row["planning_item_id"]]["inspections"].append(dict(row))
    for row in connection.execute(
        "SELECT events.item_id, events.action, events.created_at, events.details, "
        "users.display_name AS actor_name FROM planning_events AS events "
        "JOIN workflow_users AS users ON users.id = events.actor_id ORDER BY events.id"
    ):
        if row["item_id"] in by_id:
            by_id[row["item_id"]]["events"].append({
                "action": ACTION_LABELS.get(row["action"], row["action"]),
                "created_at": row["created_at"], "actor_name": row["actor_name"],
                "details_text": planning_details_text(row["details"]),
            })
    return items


def planning_details_text(details: str) -> str:
    values = json.loads(details or "{}")
    parts = []
    for key, value in values.items():
        label = LABELS.get(key, key)
        if isinstance(value, list) and len(value) == 2:
            parts.append(f"{label}: {value[0] or '(vacío)'} → {value[1] or '(vacío)'}")
        else:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


def _sync_info(connection) -> dict | None:
    row = connection.execute(
        "SELECT planning_sync.*, users.display_name AS synced_by_name FROM planning_sync "
        "LEFT JOIN workflow_users AS users ON users.id = planning_sync.synced_by WHERE planning_sync.id = 1"
    ).fetchone()
    if not row:
        return None
    return {
        "synced_at": row["synced_at"], "synced_by_name": row["synced_by_name"] or "",
        "files": json.loads(row["files"]), "errors": json.loads(row["errors"]),
        "root": row["root"], "root_exists": bool(row["root_exists"]),
        "parser_version": row["parser_version"],
    }


def _needs_sync(connection) -> bool:
    """Primera vez, o la forma de leer las planillas cambió desde la última lectura."""
    sync = _sync_info(connection)
    return sync is None or sync["parser_version"] != PARSER_VERSION


def _clean_date(value, label) -> str:
    value = (value or "").strip() if isinstance(value, str) else value
    if value in ("", None):
        return ""
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        parsed = None
    if not parsed:
        raise ValueError(f"{label}: la fecha no es válida.")
    return parsed.isoformat()


class PlanningRoutes:
    def _planning_denied(self):
        if _is_chief(self.current_user):
            return False
        self.send_error_json(
            "Solo Jefe de Inspección o un administrador pueden ver la planificación.", 403
        )
        return True

    def _send_planning(self, connection):
        today = date.today()
        self.send_json({
            "items": load_items(connection, today),
            "sync": _sync_info(connection),
            "today": today.isoformat(),
        })

    def get_inspection_planning(self):
        if self._planning_denied():
            return
        connection = connect_db()
        try:
            if _needs_sync(connection):
                # Primera vez (o lector actualizado): se cargan las planillas en la base.
                sync_planning(connection, settings.PLANNING_ROOT, self.current_user["id"])
            self._send_planning(connection)
        finally:
            connection.close()

    def sync_inspection_planning(self, data=None):
        if self._planning_denied():
            return
        connection = connect_db()
        try:
            sync_planning(connection, settings.PLANNING_ROOT, self.current_user["id"])
            self._send_planning(connection)
        finally:
            connection.close()

    def update_planning_item(self, data):
        if self._planning_denied():
            return
        item_id, version = data.get("id"), data.get("version")
        if type(item_id) is not int or type(version) is not int:
            raise ValueError("Faltan datos de la fila a modificar.")
        restore = data.get("restore") is True
        connection = connect_db()
        try:
            # La entidad se valida antes de bloquear la base (recorre carpetas) y solo
            # si cambió: una fila con una entidad renombrada sigue siendo editable.
            entity_name = None
            if not restore and "entity_name" in data:
                current = connection.execute(
                    "SELECT entity_name FROM planning_items WHERE id = ?", (item_id,)
                ).fetchone()
                requested = data.get("entity_name")
                if not (current and isinstance(requested, str)
                        and _entity_name_key(requested) == _entity_name_key(current["entity_name"])):
                    entity_name = entity_name_or_error(requested)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM planning_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                return self.send_error_json("La fila de planificación no existe.", 404)
            if row["version"] != version:
                connection.rollback()
                return self.send_error_json(
                    "Otra persona modificó esta fila. Vuelve a cargar la planificación.", 409
                )
            if entity_name is None:
                entity_name = row["entity_name"]
            changes, updates = {}, {}
            for field, limit in EDITABLE.items():
                sheet_value = row[f"sheet_{field}"]
                before = sheet_value if row[f"manual_{field}"] is None else row[f"manual_{field}"]
                if restore:
                    value = sheet_value
                else:
                    raw = data.get(field, before)
                    if not isinstance(raw, str):
                        raise ValueError(f"{LABELS[field]} no es válido.")
                    value = raw.strip()
                    if field.endswith("_inspection"):
                        value = _clean_date(value, LABELS[field])
                    elif len(value) > limit:
                        raise ValueError(f"{LABELS[field]} supera {limit} caracteres.")
                    if field == "institution" and not value:
                        raise ValueError("La institución no puede quedar vacía.")
                # Si coincide con la planilla, deja de ser una corrección manual.
                updates[f"manual_{field}"] = None if value == sheet_value else value
                if value != before:
                    changes[field] = [_day(before) if field.endswith("_inspection") else before,
                                      _day(value) if field.endswith("_inspection") else value]
            notes = row["notes"]
            if not restore:
                notes = data.get("notes", notes)
                if not isinstance(notes, str) or len(notes.strip()) > 2000:
                    raise ValueError("Las notas no son válidas (máximo 2000 caracteres).")
                notes = notes.strip()
                if notes != row["notes"]:
                    changes["notes"] = [row["notes"], notes]
                if entity_name != row["entity_name"]:
                    changes["entity_name"] = [row["entity_name"], entity_name]
            if changes:
                now = _now()
                assignments = ", ".join(f"{column} = ?" for column in updates)
                connection.execute(
                    f"UPDATE planning_items SET {assignments}, notes = ?, entity_name = ?, "
                    "version = version + 1, updated_at = ?, updated_by = ? WHERE id = ?",
                    (*updates.values(), notes, entity_name, now, self.current_user["id"], item_id),
                )
                connection.execute(
                    "INSERT INTO planning_events (item_id, actor_id, action, created_at, details) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_id, self.current_user["id"], "restore" if restore else "edit", now,
                     json.dumps(changes, ensure_ascii=False)),
                )
            connection.commit()
            self._send_planning(connection)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def export_inspection_planning(self, params):
        if not _is_chief(self.current_user):
            return self.send_error_json(
                "Solo Jefe de Inspección o un administrador pueden exportar la planificación.", 403
            )

        def param(name):
            return (params.get(name, [""])[0] or "").strip()[:200]

        status = param("status") if param("status") in STATUS_LABELS else ""
        sort = param("sort") if param("sort") in SORTS else "next"
        month = _valid_month(param("month"))
        query, category = param("q"), param("category")
        connection = connect_db()
        try:
            if _needs_sync(connection):
                sync_planning(connection, settings.PLANNING_ROOT, self.current_user["id"])
            today = date.today()
            items = load_items(connection, today)
            sync = _sync_info(connection)
        finally:
            connection.close()
        rows = filter_groups(items, query, status, category, sort, month)

        columns = [
            "Institución", "Práctica", "Departamento", "Última inspección", "Próxima inspección",
            "Estado", "Equipos", "Planilla", "Entidad", "Corregido a mano", "Notas", "Inspecciones agendadas",
            "Figura en planillas", "Hojas de origen",
        ]
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Planificación"
        sheet.append(columns)
        for item in rows:
            sheet.append([
                item["institution"], item["practice"], item["department"],
                _day(item["last_inspection"]), _day(item["next_inspection"]),
                STATUS_LABELS.get(item["status"], item["status"]), str(item["equipment_count"]),
                ", ".join(item["categories"]), item["entity_name"],
                ", ".join(LABELS[field] for field in item["edited"]),
                item["notes"], ", ".join(f"#{inspection['id']}" for inspection in item["inspections"]),
                "Sí" if item["in_sheets"] else "No", "; ".join(item["sources"]),
            ])
        filters = workbook.create_sheet("Filtros")
        for row in (
            ("Filtro", "Valor"),
            ("Búsqueda", query or "(ninguna)"),
            ("Estado", STATUS_LABELS.get(status, "Todos")),
            ("Vencen en", month_label(month) if month else "Cualquier mes"),
            ("Planilla", category or "Todas"),
            ("Orden", SORT_LABELS[sort]),
            ("Filas exportadas", f"{len(rows)} de {len(items)}"),
            ("Fecha de referencia", _day(today.isoformat())),
            ("Planillas leídas", ", ".join(item["file"] for item in sync["files"]) or "ninguna"),
            ("Última lectura de planillas (UTC)", sync["synced_at"]),
            ("Planillas con error", ", ".join(item["file"] for item in sync["errors"]) or "ninguna"),
        ):
            filters.append(row)
        widths = {
            "Planificación": [45, 28, 18, 18, 18, 20, 10, 32, 40, 30, 40, 20, 12, 60],
            "Filtros": [32, 90],
        }
        for current in (sheet, filters):
            for cell in current[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1D4ED8")
            for index, width in enumerate(widths[current.title], start=1):
                current.column_dimensions[get_column_letter(index)].width = width
            # Todo se guarda como texto: nunca se interpreta como fórmula ni número.
            for row in current.iter_rows(min_row=2):
                for cell in row:
                    if cell.value is not None:
                        cell.value = str(cell.value)
                        cell.data_type = "s"
            current.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

        output = BytesIO()
        workbook.save(output)
        content = output.getvalue()
        self.send_response(200)
        self.send_header(
            "Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", 'attachment; filename="planificacion_inspecciones.xlsx"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)
