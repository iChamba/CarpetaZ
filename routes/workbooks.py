import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from documents import file_version, safe_relative_path
from settings import APP_DATA, WORKBOOK_LOCK
from workbook_service import cell_input_value, workbook_cell_value


class WorkbookRoutes:
    def read_workbook(self, params: dict[str, list[str]]) -> None:
        path, relative = safe_relative_path(params.get("path", [""])[0])
        if path.suffix.lower() != ".xlsx":
            raise ValueError("Solo se pueden abrir libros .xlsx.")
        try:
            start_row = max(1, int(params.get("start", ["1"])[0]))
            row_count = min(100, max(1, int(params.get("rows", ["50"])[0])))
        except ValueError:
            raise ValueError("El rango de filas no es válido.") from None
        sheet_name = params.get("sheet", [""])[0]
        version = file_version(path)
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            if sheet_name and sheet_name not in workbook.sheetnames:
                raise ValueError("La hoja solicitada no existe.")
            sheet = workbook[sheet_name] if sheet_name else workbook[workbook.sheetnames[0]]
            max_row = sheet.max_row or 0
            max_column = min(sheet.max_column or 0, 50)
            rows = []
            for values in sheet.iter_rows(
                min_row=start_row,
                max_row=min(max_row, start_row + row_count - 1),
                max_col=max_column,
                values_only=False,
            ):
                rows.append([
                    {
                        "value": workbook_cell_value(cell.value),
                        "formula": isinstance(cell.value, str) and cell.value.startswith("="),
                    }
                    for cell in values
                ])
            if file_version(path) != version:
                return self.send_error_json(
                    "El Excel cambió mientras se estaba cargando. Vuelve a abrir la hoja.",
                    409,
                )
            self.send_json({
                "path": relative,
                "sheet": sheet.title,
                "sheets": workbook.sheetnames,
                "start": start_row,
                "rows": rows,
                "max_row": max_row,
                "max_column": max_column,
                "truncated_columns": (sheet.max_column or 0) > max_column,
                "version": version,
            })
        finally:
            workbook.close()

    def save_workbook(self, data: dict) -> None:
        path, relative = safe_relative_path(str(data.get("path", "")))
        if path.suffix.lower() != ".xlsx":
            raise ValueError("Solo se pueden guardar libros .xlsx.")
        sheet_name = str(data.get("sheet", ""))
        expected_version = str(data.get("version", ""))
        updates = data.get("updates")
        if not sheet_name or not expected_version or not isinstance(updates, list) or not updates:
            raise ValueError("Faltan datos para guardar los cambios.")
        if len(updates) > 1000:
            raise ValueError("Guarda hasta 1000 celdas por vez.")

        with WORKBOOK_LOCK:
            if file_version(path) != expected_version:
                return self.send_error_json(
                    "El Excel cambió desde que lo abriste. Vuelve a cargarlo para evitar sobrescribir cambios ajenos.",
                    409,
                )
            backup_dir = APP_DATA / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_name = f"{stamp}-{abs(hash(relative))}.xlsx"
            shutil.copy2(path, backup_dir / backup_name)
            if file_version(path) != expected_version:
                return self.send_error_json(
                    "El Excel cambió durante el guardado. No se aplicaron tus cambios; vuelve a cargarlo.",
                    409,
                )

            workbook = load_workbook(path, data_only=False)
            try:
                if sheet_name not in workbook.sheetnames:
                    raise ValueError("La hoja solicitada ya no existe.")
                sheet = workbook[sheet_name]
                for update in updates:
                    if not isinstance(update, dict):
                        raise ValueError("Una celda modificada no tiene un formato válido.")
                    try:
                        row = int(update["row"])
                        column = int(update["column"])
                        value = update["value"]
                    except (KeyError, TypeError, ValueError):
                        raise ValueError("Una celda modificada no tiene coordenadas válidas.") from None
                    if (
                        row < 1
                        or row > sheet.max_row
                        or column < 1
                        or column > min(sheet.max_column, 50)
                        or not isinstance(value, str)
                    ):
                        raise ValueError("Una celda modificada está fuera del rango permitido.")
                    cell = sheet.cell(row=row, column=column)
                    cell.value = cell_input_value(cell.value, value)

                with tempfile.NamedTemporaryFile(
                    prefix="documentos-",
                    suffix=".xlsx",
                    dir=str(path.parent),
                    delete=False,
                ) as temporary:
                    temporary_path = temporary.name
                try:
                    workbook.save(temporary_path)
                    if file_version(path) != expected_version:
                        return self.send_error_json(
                            "El Excel cambió mientras se preparaban los cambios. No se sobrescribió.",
                            409,
                        )
                    os.replace(temporary_path, path)
                finally:
                    temporary_file = Path(temporary_path)
                    if temporary_file.exists():
                        temporary_file.unlink()
            finally:
                workbook.close()

        self.send_json({"saved": True, "path": relative, "version": file_version(path)})
