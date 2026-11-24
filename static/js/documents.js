import {
  element,
  requestJson,
  resetPreviewActions,
  setMessage,
  setPreviewCloseAction,
  setPdfPreviewActions,
} from "./common.js";

export async function previewFile(path, scope = "user") {
  const extension = path.split(".").pop().toLowerCase();
  const content = document.getElementById(`${scope}-preview-content`);
  const title = document.getElementById(`${scope}-preview-title`);
  resetPreviewActions(title);
  title.textContent = path;
  content.className = "preview-content";
  content.replaceChildren();
  const url = `/api/preview?path=${encodeURIComponent(path)}`;

  if (extension === "pdf") {
    const frame = document.createElement("iframe");
    frame.title = `Vista previa de ${path}`;
    frame.src = url;
    content.append(frame);
    setPdfPreviewActions(title, frame, () => closePreview(scope));
    return;
  }
  setPreviewCloseAction(title, () => closePreview(scope));
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
      setMessage("user-message", error.message, "error");
    }
    return;
  }
  content.className = "preview-content empty-state";
  content.textContent = "Este formato no se puede previsualizar.";
}

function closePreview(scope) {
  const content = document.getElementById(`${scope}-preview-content`);
  const title = document.getElementById(`${scope}-preview-title`);
  content.className = "preview-content empty-state";
  content.textContent = "Selecciona un documento para verlo aquí.";
  title.textContent = "Vista previa";
  resetPreviewActions(title);

  if (scope === "user") {
    const browser = document.querySelector("#user-profile .preview-browser-layout");
    if (browser) browser.classList.add("preview-hidden");
  }
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
  { navigateToRecords = true, messageId = "user-message" } = {},
) {
  const statusMessageId = navigateToRecords ? messageId : "user-message";
  setMessage(statusMessageId, "Importando datos del documento…");
  try {
    const data = await requestJson("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const note = data.updated
      ? "Autorización individual actualizada. La ficha comparará esta versión con las anteriores."
      : "Autorización individual detectada e importada. La ficha comparará esta versión con las anteriores.";
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
