const tabButtons = document.querySelectorAll(".tab-btn");
const tabSections = document.querySelectorAll(".tab");

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    tabButtons.forEach((b) => b.classList.remove("active"));
    tabSections.forEach((s) => s.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  });
});

document.getElementById("upload-btn").addEventListener("click", async () => {
  const input = document.getElementById("file-input");
  const status = document.getElementById("upload-status");
  const preview = document.getElementById("upload-preview");

  if (!input.files.length) {
    status.textContent = "Choose a file first.";
    return;
  }

  const formData = new FormData();
  formData.append("file", input.files[0]);
  status.textContent = "Uploading & converting… (PDFs with scanned pages can take a moment)";
  preview.textContent = "";

  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    status.textContent = `Saved as document #${data.id}: ${data.filename}`;
    preview.textContent = data.markdown;
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
});

document.getElementById("search-btn").addEventListener("click", runSearch);
document.getElementById("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});

async function runSearch() {
  const q = document.getElementById("search-input").value.trim();
  const results = document.getElementById("search-results");
  results.innerHTML = "";
  if (!q) return;

  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  const data = await res.json();
  if (!data.length) {
    results.innerHTML = "<li>No matches.</li>";
    return;
  }
  data.forEach((doc) => {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${escapeHtml(doc.filename)}</strong> ` +
      `(#${doc.id}, ${doc.uploaded_at})<br>${doc.snippet}`;
    results.appendChild(li);
  });
}

document.getElementById("chat-btn").addEventListener("click", async () => {
  const instruction = document.getElementById("chat-input").value.trim();
  const status = document.getElementById("chat-status");
  const output = document.getElementById("chat-output");
  if (!instruction) return;

  status.textContent = "Compiling…";
  output.textContent = "";

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Compile failed");
    status.textContent = data.sources.length
      ? `Compiled from ${data.sources.length} source file(s): ${data.sources.join(", ")}`
      : "No matching source files.";
    output.textContent = data.markdown;
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
});

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
