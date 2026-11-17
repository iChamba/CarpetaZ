import { element, requestJson, setMessage } from "./common.js";

export async function previewFile(path, scope = "user") {
  const extension = path.split(".").pop().toLowerCase();
  const content = document.getElementById(`${scope}-preview-content`);
  const title = document.getElementById(`${scope}-preview-title`);
  const download = document.getElementById(`${scope}-download-link`);
  const messageId = scope === "search" ? "search-message" : "user-message";
  title.textContent = path;
  download.href = `/api/download?path=${encodeURIComponent(path)}`;
  download.classList.remove("hidden");
  content.className = "preview-content";
  content.replaceChildren();
  const url = `/api/preview?path=${encodeURIComponent(path)}`;

  if (extension === "pdf") {
    const frame = document.createElement("iframe");
    frame.title = `Vista previa de ${path}`;
    frame.src = url;
    content.append(frame);
    return;
  }
  if (["jpg", "jpeg", "png"].includes(extension)) {
    const image = document.createElement("img");
    image.alt = path;
    image.src = url;
    content.append(image);
    return;
  }
  if (["txt", "doc", "docx"].includes(extension)) {
    try {
      const response = await fetch(url);
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || `Error HTTP ${response.status}`);
      }
      content.append(element("pre", await response.text()));
    } catch (error) {
      setMessage(messageId, error.message, "error");
    }
    return;
  }
  content.className = "preview-content empty-state";
  content.textContent = "Este formato se puede descargar, pero no previsualizar.";
}

export async function revealFile(path, messageId = "user-message") {
  try {
    await requestJson("/api/open-location", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    setMessage(
      messageId,
      "Se abrió el Explorador en la ubicación del archivo, en la PC que ejecuta la aplicación.",
      "success",
    );
  } catch (error) {
    setMessage(messageId, error.message, "error");
  }
}

export async function revealFolder(name, messageId = "records-message") {
  try {
    await requestJson("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    setMessage(
      messageId,
      "Se abrió la carpeta en el Explorador, en la PC que ejecuta la aplicación.",
      "success",
    );
  } catch (error) {
    setMessage(messageId, error.message, "error");
  }
}

export async function extractRecord(
  path,
  { navigateToRecords = true, messageId = "search-message" } = {},
) {
  const statusMessageId = navigateToRecords ? messageId : "user-message";
  setMessage(statusMessageId, "Importando datos del documento…");
  try {
    const data = await requestJson("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    let note = "Campos candidatos agregados. Revisa y corrige la fila antes de exportar.";
    if (data.record.is_authorization_certificate) {
      note = data.updated
        ? "Autorización individual actualizada. La ficha comparará esta versión con las anteriores."
        : "Autorización individual detectada e importada. La ficha comparará esta versión con las anteriores.";
    } else if (data.updated) {
      note = "Campos del certificado actualizados. Se conservaron los demás datos de la ficha.";
    } else if (data.already_extracted) {
      note = "Ese PDF ya estaba en la tabla; se conservaron tus ediciones.";
    }
    if (navigateToRecords) {
      setMessage("records-message", note, "success");
      document.querySelector('[data-tab="records"]').click();
    } else {
      setMessage("user-message", note, "success");
    }
    return note;
  } catch (error) {
    setMessage(statusMessageId, error.message, "error");
    return null;
  }
}
