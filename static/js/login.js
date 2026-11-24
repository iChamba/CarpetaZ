const form = document.getElementById("login-form");
const message = document.getElementById("login-message");

form.addEventListener("submit", async event => {
  event.preventDefault();
  message.textContent = "";
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Error HTTP ${response.status}`);
    window.location.assign("/");
  } catch (error) {
    message.textContent = error.message || "No se pudo iniciar sesión.";
  }
});
