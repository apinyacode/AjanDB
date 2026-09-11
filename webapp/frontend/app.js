// --- API keys: browser-local only, never sent anywhere but this page's own backend ---
const LS_ANTHROPIC_KEY = "ajandb_anthropic_key";
const LS_OPENAI_KEY = "ajandb_openai_key";
const anthropicKeyInput = document.getElementById("anthropic-key-input");
const openaiKeyInput = document.getElementById("openai-key-input");
const keyStatus = document.getElementById("key-status");

function safeLocalStorage() {
  try {
    localStorage.setItem("__ajandb_test__", "1");
    localStorage.removeItem("__ajandb_test__");
    return localStorage;
  } catch {
    return null;
  }
}
const storage = safeLocalStorage();

function loadStoredKeys() {
  if (!storage) {
    keyStatus.textContent = "Browser storage unavailable — keys won't be remembered between visits.";
    return;
  }
  anthropicKeyInput.value = storage.getItem(LS_ANTHROPIC_KEY) || "";
  openaiKeyInput.value = storage.getItem(LS_OPENAI_KEY) || "";
}
loadStoredKeys();

function persistKey(input, storageKey) {
  input.addEventListener("input", () => {
    if (!storage) return;
    if (input.value) storage.setItem(storageKey, input.value);
    else storage.removeItem(storageKey);
  });
}
persistKey(anthropicKeyInput, LS_ANTHROPIC_KEY);
persistKey(openaiKeyInput, LS_OPENAI_KEY);

document.getElementById("clear-keys-btn").addEventListener("click", () => {
  anthropicKeyInput.value = "";
  openaiKeyInput.value = "";
  if (storage) {
    storage.removeItem(LS_ANTHROPIC_KEY);
    storage.removeItem(LS_OPENAI_KEY);
  }
  keyStatus.textContent = "Cleared.";
  setTimeout(() => { keyStatus.textContent = ""; }, 2000);
});

function currentApiKeys() {
  return {
    anthropic_api_key: anthropicKeyInput.value.trim() || undefined,
    openai_api_key: openaiKeyInput.value.trim() || undefined,
  };
}

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

const engineSelect = document.getElementById("engine-select");
const uploadProviderSelect = document.getElementById("upload-provider-select");
engineSelect.addEventListener("change", () => {
  uploadProviderSelect.disabled = engineSelect.value !== "vision";
});

document.getElementById("upload-btn").addEventListener("click", async () => {
  const input = document.getElementById("file-input");
  const status = document.getElementById("upload-status");
  const chunksEl = document.getElementById("upload-chunks");
  const categoryInput = document.getElementById("category-input");

  if (!input.files.length) {
    status.textContent = "Choose a file first.";
    return;
  }

  const engine = engineSelect.value;
  const formData = new FormData();
  formData.append("file", input.files[0]);
  formData.append("engine", engine);
  if (engine === "vision") formData.append("provider", uploadProviderSelect.value);
  if (categoryInput.value.trim()) formData.append("category", categoryInput.value.trim());
  const keys = currentApiKeys();
  if (keys.anthropic_api_key) formData.append("anthropic_api_key", keys.anthropic_api_key);
  if (keys.openai_api_key) formData.append("openai_api_key", keys.openai_api_key);

  status.textContent = engine === "vision"
    ? "Uploading & converting via vision-LLM… (one API call per scanned page, can take a while)"
    : "Uploading & converting… (PDFs with scanned pages can take a moment)";
  chunksEl.innerHTML = "";

  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    const flagged = data.chunks.filter((c) => c.needs_review).length;
    status.textContent = `Saved "${escapeHtml(data.source_filename)}" as ${data.chunks.length} chunk(s), ` +
      `category "${escapeHtml(data.category)}"` +
      (flagged ? ` — ${flagged} chunk(s) flagged for review.` : ".");
    categoryInput.value = data.category;
    renderChunks(chunksEl, data.chunks);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
});

function renderChunks(container, chunks) {
  container.innerHTML = "";
  chunks.forEach((chunk) => {
    const card = document.createElement("div");
    card.className = "chunk-card" + (chunk.needs_review ? " needs-review" : "");
    const confidenceText = chunk.confidence === null || chunk.confidence === undefined
      ? "n/a" : `${chunk.confidence}%`;
    card.innerHTML =
      `<div class="chunk-meta">` +
      `<span>Page ${chunk.page_number}</span>` +
      `<span>Confidence: ${confidenceText}</span>` +
      (chunk.needs_review ? `<span class="badge">Needs review</span>` : "") +
      `</div>` +
      `<pre class="markdown-preview">${escapeHtml(chunk.markdown)}</pre>`;
    container.appendChild(card);
  });
}

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
    const confidenceText = doc.confidence === null || doc.confidence === undefined
      ? "n/a" : `${doc.confidence}%`;
    li.innerHTML =
      `<strong>${escapeHtml(doc.source_filename)}</strong> ` +
      `<span class="doc-meta">page ${doc.page_number}/${doc.total_pages} · ` +
      `${escapeHtml(doc.category)} · confidence ${confidenceText}` +
      (doc.needs_review ? ` · <span class="badge">Needs review</span>` : "") +
      `</span><br>${doc.snippet}`;
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

  const provider = document.getElementById("chat-provider-select").value;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction, provider, ...currentApiKeys() }),
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
