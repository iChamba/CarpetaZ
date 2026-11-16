from datetime import date, datetime


def workbook_cell_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def cell_input_value(current: object, value: str) -> object:
    if len(value) > 5000:
        raise ValueError("Cada celda admite hasta 5000 caracteres.")
    if isinstance(current, str) and current.startswith("="):
        raise ValueError("No se permite sobrescribir una celda con fórmula desde esta vista.")
    if value == "":
        return None
    if isinstance(current, bool):
        if value.lower() not in {"true", "false", "verdadero", "falso"}:
            raise ValueError("Escribe verdadero o falso en esta celda.")
        return value.lower() in {"true", "verdadero"}
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except ValueError:
            raise ValueError("El valor debe ser un número entero.") from None
    if isinstance(current, float):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            raise ValueError("El valor debe ser numérico.") from None
    if isinstance(current, (datetime, date)):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("La fecha debe tener formato AAAA-MM-DD.") from None
    if value.startswith("="):
        raise ValueError("No se permiten fórmulas nuevas desde esta vista.")
    return value
