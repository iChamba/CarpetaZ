"""Ficha de cada entidad usuaria y sus equipos (filas de planificación).

La ficha junta datos de contacto que se cargan a mano o que llegan desde las
inspecciones asociadas a la entidad. Lo que llega de una inspección solo completa
campos vacíos: nunca reemplaza un dato ya cargado.
"""

import json
import re
from datetime import date, datetime, timezone

from database import connect_db
from inspection_planning import normalize, status_for
from routes.entities import _entity_name_key
from routes.inspections import _can_manage, _is_chief, entity_name_or_error

PROFILE_FIELDS = {
    "rut": 100, "address": 255, "city": 160, "department": 160,
    "phone": 100, "email": 254, "contact_details": 255, "notes": 2000,
}
PROFILE_LABELS = {
    "rut": "RUT", "address": "Domicilio", "city": "Ciudad", "department": "Departamento",
    "phone": "Teléfono", "email": "Email", "contact_details": "Datos de contacto",
    "notes": "Notas", "equipment": "Equipos",
}
# Datos de la inspección que completan la ficha (mismo nombre en ambos lados). El
# departamento llega solo cuando la inspección se agenda desde la planificación.
FROM_INSPECTION = ("rut", "address", "city", "department", "phone", "email", "contact_details")
ACTION_LABELS = {
    "edit": "Ficha editada", "from_inspection": "Completada desde una inspección",
    "link_equipment": "Equipos asociados", "unlink_equipment": "Equipos desasociados",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compact(value) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(value))


# Palabras que no alcanzan para decir que dos nombres son la misma institución.
STOPWORDS = {"de", "del", "la", "las", "el", "los", "y", "e", "en", "sa", "srl", "ltda"}
GENERIC_WORDS = {
    "sanatorio", "hospital", "clinica", "policlinica", "policlinico", "centro", "medico",
    "medica", "laboratorio", "servicio", "servicios", "instituto", "asociacion",
    "cooperativa", "mutualista", "odontologia", "odontologico", "radiologia",
    "radiologico", "diagnostico", "sede", "sucursal", "empresa", "industria",
}


def _words(value) -> list[str]:
    return [word for word in re.split(r"[^a-z0-9]+", normalize(value)) if word]


def _contains(longer: list[str], shorter: list[str]) -> bool:
    size = len(shorter)
    return any(longer[index:index + size] == shorter for index in range(len(longer) - size + 1))


def name_match_kind(entity: str, institution: str) -> str:
    """"exact" si son el mismo nombre sin puntuación, "partial" si uno contiene al
    otro por palabras completas y con nombre propio suficiente, "" si no coinciden."""
    a, b = _compact(entity), _compact(institution)
    if not a or not b:
        return ""
    if a == b:
        return "exact"
    shorter, longer = sorted((_words(entity), _words(institution)), key=len)
    if not shorter or not _contains(longer, shorter) or len("".join(shorter)) < 6:
        return ""
    proper = [word for word in shorter if word not in STOPWORDS and word not in GENERIC_WORDS]
    # «Hospital» o «Centro Médico» solos son demasiado genéricos.
    if not proper or (len(shorter) == 1 and len(shorter[0]) < 6):
        return ""
    return "partial"


def names_match(entity: str, institution: str) -> bool:
    """Coincidencia para sugerir equipos: igual sin puntuación, o uno contiene al otro."""
    return bool(name_match_kind(entity, institution))


def _profile_row(connection, entity_name, create=False, now=None):
    key = _entity_name_key(entity_name)
    row = connection.execute("SELECT * FROM entity_profiles WHERE entity_key = ?", (key,)).fetchone()
    if row or not create:
        return row
    connection.execute(
        "INSERT INTO entity_profiles (entity_key, entity_name, created_at) VALUES (?, ?, ?)",
        (key, entity_name, now or _now()),
    )
    return connection.execute("SELECT * FROM entity_profiles WHERE entity_key = ?", (key,)).fetchone()


def _event(connection, profile_id, actor_id, action, details, now):
    connection.execute(
        "INSERT INTO entity_profile_events (profile_id, actor_id, action, created_at, details) "
        "VALUES (?, ?, ?, ?, ?)",
        (profile_id, actor_id, action, now, json.dumps(details, ensure_ascii=False)),
    )


def fill_profile_from_inspection(connection, entity_name, values, inspection_id, actor_id):
    """Completa los campos vacíos de la ficha con los datos de una inspección.

    Se llama dentro de la transacción que crea la inspección o le asocia la entidad.
    """
    if not entity_name:
        return
    now = _now()
    incoming = {field: (values[field] or "").strip() for field in FROM_INSPECTION
                if field in values.keys() and (values[field] or "").strip()}
    if not incoming:
        return
    profile = _profile_row(connection, entity_name, create=True, now=now)
    changes = {field: ["", value] for field, value in incoming.items() if not profile[field]}
    if not changes:
        return
    assignments = ", ".join(f"{field} = ?" for field in changes)
    connection.execute(
        f"UPDATE entity_profiles SET {assignments}, version = version + 1, updated_at = ?, "
        "updated_by = ? WHERE id = ?",
        (*(value for _, value in changes.values()), now, actor_id, profile["id"]),
    )
    changes["inspection"] = f"#{inspection_id}"
    _event(connection, profile["id"], actor_id, "from_inspection", changes, now)


def _details_text(details: str) -> str:
    parts = []
    for key, value in json.loads(details or "{}").items():
        label = PROFILE_LABELS.get(key, "Inspección" if key == "inspection" else key)
        if isinstance(value, list) and len(value) == 2:
            parts.append(f"{label}: {value[0] or '(vacío)'} → {value[1] or '(vacío)'}")
        else:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


def _planning_rows(connection, today):
    rows = connection.execute(
        "SELECT id, entity_name, in_sheets, sheet_equipment_count AS equipment_count, categories, "
        "COALESCE(manual_institution, sheet_institution) AS institution, "
        "COALESCE(manual_practice, sheet_practice) AS practice, "
        "COALESCE(manual_department, sheet_department) AS department, "
        "COALESCE(manual_last_inspection, sheet_last_inspection) AS last_inspection, "
        "COALESCE(manual_next_inspection, sheet_next_inspection) AS next_inspection "
        "FROM planning_items"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["categories"] = json.loads(item["categories"])
        item["in_sheets"] = bool(item["in_sheets"])
        item["status"] = status_for(item["next_inspection"], today)
        result.append(item)
    return result


class EntityProfileRoutes:
    def _send_entity_record(self, connection, entity_name):
        today = date.today()
        profile = _profile_row(connection, entity_name)
        events = []
        if profile:
            events = [
                {
                    "action": ACTION_LABELS.get(row["action"], row["action"]),
                    "created_at": row["created_at"], "actor_name": row["actor_name"],
                    "details_text": _details_text(row["details"]),
                }
                for row in connection.execute(
                    "SELECT events.*, users.display_name AS actor_name FROM entity_profile_events AS events "
                    "JOIN workflow_users AS users ON users.id = events.actor_id "
                    "WHERE events.profile_id = ? ORDER BY events.id DESC LIMIT 50", (profile["id"],),
                )
            ]
        key = _entity_name_key(entity_name)
        equipment, suggested = [], []
        # Los equipos salen de la planificación, que solo ven Jefe de Inspección y administradores.
        can_view_equipment = _is_chief(self.current_user)
        for item in _planning_rows(connection, today) if can_view_equipment else ():
            if item["entity_name"]:
                if _entity_name_key(item["entity_name"]) == key:
                    equipment.append(item)
            else:
                item["match"] = name_match_kind(entity_name, item["institution"])
                if item["match"]:
                    suggested.append(item)
        order = lambda item: (item["next_inspection"] or "9999", normalize(item["practice"]))
        equipment.sort(key=order)
        suggested.sort(key=order)
        self.send_json({
            "entity_name": entity_name,
            "profile": {
                **{field: (profile[field] if profile else "") for field in PROFILE_FIELDS},
                "version": profile["version"] if profile else 0,
                "updated_at": profile["updated_at"] if profile else "",
            },
            "events": events,
            "equipment": equipment,
            "suggested_equipment": suggested,
            "can_edit": _can_manage(self.current_user),
            "can_view_equipment": can_view_equipment,
            "can_link": can_view_equipment,
            "today": today.isoformat(),
        })

    def get_entity_record(self, params):
        entity_name = entity_name_or_error(params.get("name", [""])[0])
        if not entity_name:
            raise ValueError("Indica la entidad.")
        connection = connect_db()
        try:
            self._send_entity_record(connection, entity_name)
        finally:
            connection.close()

    def update_entity_record(self, data):
        if not _can_manage(self.current_user):
            return self.send_error_json(
                "Solo Secretaría, Jefe de Inspección o un administrador editan la ficha.", 403
            )
        entity_name = entity_name_or_error(data.get("name"))
        version = data.get("version")
        if not entity_name or type(version) is not int:
            raise ValueError("Faltan datos de la entidad.")
        values = {}
        for field, limit in PROFILE_FIELDS.items():
            value = data.get(field, "")
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise ValueError(f"{PROFILE_LABELS[field]}: máximo {limit} caracteres.")
            values[field] = value.strip()
        if values["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+", values["email"]):
            raise ValueError("El correo no es válido.")
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            profile = _profile_row(connection, entity_name)
            current_version = profile["version"] if profile else 0
            if current_version != version:
                connection.rollback()
                return self.send_error_json(
                    "Otra persona modificó la ficha. Vuelve a abrirla.", 409
                )
            profile = profile or _profile_row(connection, entity_name, create=True, now=now)
            changes = {field: [profile[field], value] for field, value in values.items()
                       if profile[field] != value}
            if changes:
                assignments = ", ".join(f"{field} = ?" for field in changes)
                connection.execute(
                    f"UPDATE entity_profiles SET {assignments}, version = version + 1, "
                    "updated_at = ?, updated_by = ? WHERE id = ?",
                    (*(new for _, new in changes.values()), now, self.current_user["id"], profile["id"]),
                )
                _event(connection, profile["id"], self.current_user["id"], "edit", changes, now)
            connection.commit()
            self._send_entity_record(connection, entity_name)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def link_entity_equipment(self, data):
        if not _is_chief(self.current_user):
            return self.send_error_json(
                "Solo Jefe de Inspección o un administrador asocian equipos.", 403
            )
        entity_name = entity_name_or_error(data.get("name"))
        item_ids = data.get("item_ids")
        action = data.get("action", "link")
        if action not in ("link", "unlink"):
            raise ValueError("La acción indicada no es válida.")
        link = action == "link"
        if (not entity_name or not isinstance(item_ids, list) or not item_ids
                or len(item_ids) > 500 or any(type(item) is not int for item in item_ids)):
            raise ValueError("Selecciona los equipos a asociar.")
        connection = connect_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            key = _entity_name_key(entity_name)
            names = []
            for item_id in dict.fromkeys(item_ids):
                row = connection.execute(
                    "SELECT id, entity_name, COALESCE(manual_institution, sheet_institution) AS institution, "
                    "COALESCE(manual_practice, sheet_practice) AS practice FROM planning_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if not row:
                    raise ValueError("Uno de los equipos ya no existe.")
                current = row["entity_name"]
                if link and current and _entity_name_key(current) != key:
                    raise ValueError(
                        f"{row['institution']} · {row['practice']} ya está asociado a {current}."
                    )
                if not link and _entity_name_key(current or "") != key:
                    continue
                new_value = entity_name if link else ""
                if current == new_value:
                    continue
                connection.execute(
                    "UPDATE planning_items SET entity_name = ?, version = version + 1, "
                    "updated_at = ?, updated_by = ? WHERE id = ?",
                    (new_value, now, self.current_user["id"], item_id),
                )
                connection.execute(
                    "INSERT INTO planning_events (item_id, actor_id, action, created_at, details) "
                    "VALUES (?, ?, 'edit', ?, ?)",
                    (item_id, self.current_user["id"], now,
                     json.dumps({"entity_name": [current, new_value]}, ensure_ascii=False)),
                )
                names.append(f"{row['institution']} · {row['practice']}")
            if names:
                profile = _profile_row(connection, entity_name, create=True, now=now)
                _event(connection, profile["id"], self.current_user["id"],
                       "link_equipment" if link else "unlink_equipment",
                       {"equipment": ", ".join(names)}, now)
            connection.commit()
            self._send_entity_record(connection, entity_name)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
