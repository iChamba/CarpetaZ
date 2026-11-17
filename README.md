# Documentos locales

Aplicación web local para explorar una carpeta de documentos, buscar en el texto de los PDF, revisar campos candidatos y exportarlos a Excel. También permite editar libros `.xlsx`.

## Estructura del proyecto

- `app.py`: punto de entrada pequeño; inicia `server.py`.
- `settings.py` y `database.py`: configuración, estado compartido y SQLite.
- `documents.py`, `extraction.py` y `workbook_service.py`: lógica de archivos y procesamiento, separada del HTTP.
- `routes/`: manejadores de API agrupados por documentos, registros y libros Excel.
- `server.py`: autenticación, envío de respuestas y despacho de rutas.
- `static/js/`: interfaz JavaScript dividida en módulos comunes, documentos, búsqueda, usuarios, registros, libros y arranque.
- `static/index.html` y `static/styles.css`: estructura y estilos de la interfaz.
- `tests/`: pruebas unitarias de extracción y validaciones.

## Alcance actual

- La ficha de cada usuario muestra la lista de documentos a la izquierda; el previsualizador a la derecha aparece al pulsar **Ver** y admite PDF, DOC, DOCX, TXT e imágenes JPG/JPEG/PNG. Los archivos Word 2003 `.doc` se leen usando Microsoft Word instalado localmente.
- La acción **Ver en carpeta** abre el Explorador de Windows en el equipo que ejecuta la aplicación y selecciona el archivo en la carpeta compartida.
- Busca palabras y números en PDF con texto seleccionable. La búsqueda divide consultas en términos y exige que todos aparezcan.
- Presenta una lista de usuarios a partir de las carpetas de primer nivel. Cada ficha muestra sus documentos y los datos importados.
- Permite filtrar usuarios con o sin autorización individual y, para quienes tienen certificado, por estado: vencida, próxima a vencerse, vigente, sin fecha o pendiente de importar.
- Presenta la autorización individual en una tarjeta con titular, cédula, número, práctica, fechas y equipos. Si no encuentra certificado, indica que el usuario no tiene una autorización individual y mantiene visible la lista de documentos.
- La pestaña **Datos extraídos** muestra una fila por usuario con los datos de su autorización más reciente y un botón para verla; si no hay una autorización importada, permite abrir la carpeta del usuario. Incluye un resumen de estados y una tabla con desplazamiento horizontal.
- Las pestañas conservan los datos ya cargados al cambiar entre ellas. **Datos extraídos** permite pedir una actualización manual cuando se necesite.
- Las listas de usuarios e instituciones se muestran en páginas de hasta 100 elementos. Las fichas cargan hasta 100 documentos de usuario o 50 documentos por área de entidad, con controles para avanzar. En **Usuarios** o **Entidades usuarias**, usa **Actualizar catálogo** para detectar cambios en las carpetas compartidas.
- Ordena las autorizaciones por fecha de expedición descendente. El estado de la autorización más nueva se calcula frente a la fecha local: vencida, próxima a vencer (hasta dos meses) o vigente.
- Importa por separado candidatos de nombre, apellido, cédula, autorización y equipos desde PDF, DOC, DOCX y TXT. Para certificados `Certificado_Autorizacion_Individual_ARNR` y archivos de renovación de autorización individual, extrae los campos disponibles y conserva el número completo, incluidos sus contadores de renovación y modificación (por ejemplo, `0100/1/2`).
- Al importar un PDF, también reconoce una autorización individual por su contenido aunque el nombre del archivo no coincida; la ficha guarda esa clasificación y usa la autorización más reciente, comparando versión y fecha.
- La pestaña **Usuarios** revisa en segundo plano los PDF, DOCX y TXT de cada usuario, detecta autorizaciones por nombre o contenido e importa o actualiza las encontradas. Muestra el progreso, los archivos sin texto y los errores.
- La pestaña **Entidades usuarias** agrupa instituciones aunque aparezcan en varias áreas y permite filtrarlas por práctica, departamento y localidad opcional. Las carpetas numeradas dentro de un centro se tratan como divisiones internas, no como instituciones separadas. En la ficha, departamento y localidad aparecen junto al nombre, y las rutas de documentos se muestran relativas a la carpeta del centro. El previsualizador a la derecha aparece al pulsar **Ver**. Las licencias con formato `LO-X-XXXX-XXXX-XX` se asocian a la práctica; la revisión masiva procesa solo los documentos de la carpeta `1. Autorización` de cada institución. La extracción admite PDF, DOC, DOCX y TXT. Si la carpeta se está copiando, actualiza la lista y vuelve a procesar al terminar; estos datos se almacenan separados de los registros de usuarios.
- Guarda los registros extraídos en una base SQLite local de la aplicación y los descarga como `.xlsx`.
- Agrupa los registros y el Excel exportado por el primer nivel de carpeta, que se usa como identificador de usuario (por ejemplo, `USUARIOejemplo`).
- Permite editar celdas de archivos `.xlsx`; crea una copia de respaldo antes de cada guardado y evita guardar si el libro cambió desde que se abrió.
- Los PDF escaneados sin capa de texto y las imágenes necesitan OCR, que no está incluido en esta versión.

La carpeta de primer nivel identifica al usuario; cada documento importado queda como un registro separado. La extracción depende de las etiquetas y del texto interno del archivo. Si no se encuentra una etiqueta reconocible, el campo queda vacío para revisión. En los certificados reconocidos, cada equipo se conserva como una línea multilínea con su tipo, fabricante, modelo, radiación y práctica.

## Prueba local segura

1. Instala Python 3.10 o superior en la PC donde se ejecutará la aplicación.
2. Ejecuta `setup.bat` una vez.
3. Copia los documentos que quieras probar a una carpeta local de prueba. No selecciones la carpeta compartida original durante esta etapa.
4. Ejecuta `start_local.bat` y escribe la ruta de esa copia, por ejemplo `C:\Pruebas\Documentos`.
5. Abre `http://127.0.0.1:8765` en esa misma PC.

La carpeta indicada en `DOCUMENT_ROOT` es la fuente de archivos: la aplicación no copia ni mueve documentos. Las modificaciones de Excel sí se guardan en esa carpeta; en la prueba local, por eso debe ser una copia. Las copias de respaldo de Excel y la base de datos quedan dentro de `documentos_locales\appdata`.

La carpeta de entidades se configura con `ENTITY_ROOT`. Si no se define, se usa `3 Entidades Usuarias` junto a la carpeta `documentos_locales`.

Para ejecutar las pruebas incluidas, abre una terminal en esta carpeta y ejecuta `.venv\Scripts\python.exe -m unittest discover -s tests -v`.

## Uso desde varias PC

Para la etapa posterior, ejecuta la aplicación en la PC que permanece encendida y que tiene acceso a la carpeta SMB. Usa `start_lan.bat` y proporciona una ruta como `\\PC-SERVIDOR\Documentos`; configura una contraseña y comparte la dirección `http://IP-DEL-SERVIDOR:8765` con las demás PC de la red local. El usuario de acceso inicial es `admin`.

La autenticación usa el diálogo HTTP Basic del navegador. La contraseña se solicita al iniciar y no se guarda en el archivo `.bat`. No expongas el puerto a Internet ni lo uses en redes públicas: HTTP no cifra el tráfico. Para documentos sensibles, utiliza una VPN o publica el servicio detrás de un proxy HTTPS antes de habilitar el acceso en red. En el Firewall de Windows permite el puerto solo en redes privadas y solo para los dispositivos que correspondan.

El servidor opera con los permisos de Windows de la cuenta que lo ejecuta. Esa cuenta necesita permisos de lectura para listar, previsualizar y buscar, y permisos de escritura si se desea editar libros Excel.

## Catálogo, búsqueda e índice

Al iniciar, la aplicación cataloga los archivos compatibles de documentos y entidades e indexa los PDF en segundo plano. Las vistas consultan ese catálogo local en vez de volver a recorrer las carpetas compartidas. El catálogo registra tamaño y fecha de modificación, y el índice PDF evita reextraer texto de archivos que no cambiaron. El primer escaneo puede tardar según la cantidad de archivos; el estado aparece en la parte superior. Usa **Actualizar catálogo** para reflejar cambios en las listas y **Actualizar índice** para volver a revisar los PDF.

El índice y los datos extraídos son locales a esta instalación. No se envían a servicios externos. La búsqueda no realiza OCR.

## Límites de Excel

- Solo se editan libros `.xlsx`, hasta 50 columnas por hoja. La vista muestra 50 filas por página.
- Las fórmulas se muestran como solo lectura; no se pueden crear o reemplazar desde la vista web.
- Cada guardado genera una copia de respaldo en `appdata\backups`.
- Prueba con copias de libros que contengan gráficos u objetos especiales: la biblioteca de edición puede no conservar todos los componentes avanzados de Excel.
- El Excel exportado incluye una fila por documento importado y los equipos en una celda multilínea.
