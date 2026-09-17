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

const reviewModeCheckbox = document.getElementById("review-mode-checkbox");

document.getElementById("upload-btn").addEventListener("click", async () => {
  const input = document.getElementById("file-input");
  const status = document.getElementById("upload-status");
  const chunksEl = document.getElementById("upload-chunks");
  const categoryInput = document.getElementById("category-input");

  if (!input.files.length) {
    status.textContent = "Choose a file first.";
    return;
  }

  if (reviewModeCheckbox.checked) {
    const file = input.files[0];
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      status.textContent = "Review mode only supports PDF files.";
      return;
    }
    await startReview(file);
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

  chunksEl.innerHTML = "";
  const uploadBtn = document.getElementById("upload-btn");
  uploadBtn.disabled = true;
  const startedAt = Date.now();

  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");

    // Conversion runs in the background and can take a long time for
    // scanned PDFs (real OCR, not instant) - poll for the result instead of
    // waiting on one long-lived request, which tunnels/proxies tend to cut
    // off after a few minutes.
    while (true) {
      const elapsed = Math.round((Date.now() - startedAt) / 1000);
      status.textContent = engine === "vision"
        ? `Converting via vision-LLM… (${elapsed}s elapsed, one API call per scanned page)`
        : `Converting… (${elapsed}s elapsed)`;
      await new Promise((r) => setTimeout(r, 2000));

      const pollRes = await fetch(`/api/upload/${data.job_id}`);
      const pollData = await pollRes.json();
      if (!pollRes.ok) throw new Error(pollData.detail || "Conversion failed");
      if (pollData.status === "processing") continue;

      const flagged = pollData.chunks.filter((c) => c.needs_review).length;
      status.textContent = `Saved "${escapeHtml(pollData.source_filename)}" as ${pollData.chunks.length} chunk(s), ` +
        `category "${escapeHtml(pollData.category)}"` +
        (flagged ? ` — ${flagged} chunk(s) flagged for review.` : ".");
      categoryInput.value = pollData.category;
      renderChunks(chunksEl, pollData.chunks);
      break;
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  } finally {
    uploadBtn.disabled = false;
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

// --- Page-by-page review mode: convert one page at a time, show the scan
// next to the editable converted text, and save only on explicit approval. ---
const reviewPanel = document.getElementById("review-panel");
const reviewProgress = document.getElementById("review-progress");
const reviewBadges = document.getElementById("review-badges");
const reviewImage = document.getElementById("review-image");
const reviewMarkdown = document.getElementById("review-markdown");
const reviewApproveBtn = document.getElementById("review-approve-btn");
const reviewSkipBtn = document.getElementById("review-skip-btn");
const reviewRetryBtn = document.getElementById("review-retry-btn");
const reviewCancelBtn = document.getElementById("review-cancel-btn");
const reviewStatus = document.getElementById("review-status");

let reviewSessionId = null;
let reviewTotalPages = 0;

function setReviewControlsEnabled(enabled) {
  document.getElementById("file-input").disabled = !enabled;
  document.getElementById("upload-btn").disabled = !enabled;
  reviewModeCheckbox.disabled = !enabled;
}

async function startReview(file) {
  const status = document.getElementById("upload-status");
  const categoryInput = document.getElementById("category-input");
  const engine = engineSelect.value;
  const formData = new FormData();
  formData.append("file", file);
  formData.append("engine", engine);
  if (engine === "vision") formData.append("provider", uploadProviderSelect.value);
  if (categoryInput.value.trim()) formData.append("category", categoryInput.value.trim());
  const keys = currentApiKeys();
  if (keys.anthropic_api_key) formData.append("anthropic_api_key", keys.anthropic_api_key);
  if (keys.openai_api_key) formData.append("openai_api_key", keys.openai_api_key);

  status.textContent = "Converting page 1…";
  setReviewControlsEnabled(false);
  try {
    const res = await fetch("/api/review/start", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to start review");
    reviewSessionId = data.session_id;
    reviewTotalPages = data.total_pages;
    status.textContent = "";
    document.getElementById("upload-chunks").innerHTML = "";
    showReviewPage(data.page);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    setReviewControlsEnabled(true);
  }
}

function showReviewPage(page) {
  reviewPanel.classList.remove("hidden");
  reviewRetryBtn.classList.add("hidden");
  reviewApproveBtn.disabled = false;
  reviewSkipBtn.disabled = false;
  reviewProgress.textContent = `Page ${page.page_number} of ${reviewTotalPages}`;
  const confidenceText = page.confidence === null || page.confidence === undefined
    ? "n/a" : `${page.confidence}%`;
  reviewBadges.innerHTML =
    `Confidence: ${confidenceText}` +
    (page.needs_review ? ` <span class="badge">Needs review</span>` : "");
  reviewImage.src = `data:image/png;base64,${page.image_base64}`;
  reviewMarkdown.value = page.markdown;
  reviewStatus.textContent = "";
}

function endReviewSession(message) {
  reviewPanel.classList.add("hidden");
  reviewSessionId = null;
  setReviewControlsEnabled(true);
  document.getElementById("upload-status").textContent = message;
}

async function reviewAction(path, body) {
  reviewApproveBtn.disabled = true;
  reviewSkipBtn.disabled = true;
  reviewRetryBtn.classList.add("hidden");
  reviewStatus.textContent = "Converting next page…";
  try {
    const res = await fetch(`/api/review/${reviewSessionId}/${path}`, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");
    if (data.done) {
      endReviewSession(
        `Review complete: ${data.total_saved} page(s) saved, category "${escapeHtml(data.category)}".`);
    } else {
      showReviewPage(data.page);
    }
  } catch (err) {
    reviewStatus.textContent = `Error: ${err.message}`;
    reviewRetryBtn.classList.remove("hidden");
    reviewApproveBtn.disabled = true;
    reviewSkipBtn.disabled = true;
  }
}

reviewApproveBtn.addEventListener("click", () => {
  reviewAction("approve", { markdown: reviewMarkdown.value });
});
reviewSkipBtn.addEventListener("click", () => {
  reviewAction("skip", null);
});
reviewRetryBtn.addEventListener("click", () => {
  reviewAction("retry", null);
});
reviewCancelBtn.addEventListener("click", async () => {
  if (!reviewSessionId) return;
  try {
    const res = await fetch(`/api/review/${reviewSessionId}/cancel`, { method: "POST" });
    const data = await res.json();
    endReviewSession(`Review cancelled: ${data.total_saved} page(s) already saved.`);
  } catch (err) {
    endReviewSession(`Review cancelled (with an error checking final state: ${err.message}).`);
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
