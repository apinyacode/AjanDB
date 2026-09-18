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
  renderConsole([]);
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
    // off after a few minutes. Each poll also carries the log lines
    // accumulated so far (page 1 converting, page 2 converting, ... saved),
    // so the console below fills in live instead of only showing a result
    // at the very end.
    while (true) {
      const elapsed = Math.round((Date.now() - startedAt) / 1000);
      status.textContent = engine === "vision"
        ? `Converting via vision-LLM… (${elapsed}s elapsed, one API call per scanned page)`
        : `Converting… (${elapsed}s elapsed)`;
      await new Promise((r) => setTimeout(r, 2000));

      const pollRes = await fetch(`/api/upload/${data.job_id}`);
      const pollData = await pollRes.json();
      if (!pollRes.ok) throw new Error(pollData.detail || "Conversion failed");
      renderConsole(pollData.log);
      if (pollData.status === "processing") continue;

      const flagged = pollData.chunks.filter((c) => c.needs_review).length;
      status.textContent = `Saved "${escapeHtml(pollData.source_filename)}" as ${pollData.chunks.length} chunk(s), ` +
        `category "${escapeHtml(pollData.category)}"` +
        (flagged ? ` — ${flagged} chunk(s) flagged for review.` : ".");
      categoryInput.value = pollData.category;
      renderChunks(chunksEl, pollData.chunks);
      loadBooks();
      break;
    }
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  } finally {
    uploadBtn.disabled = false;
  }
});

function renderConsole(lines) {
  const el = document.getElementById("upload-console");
  if (!lines || !lines.length) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.classList.remove("hidden");
  el.textContent = lines.join("\n");
  el.scrollTop = el.scrollHeight;
}

// Renders `text` as HTML with every occurrence of any snippet in `groups`
// wrapped in <mark> - shared between the read-only chunk-card preview below
// and the review-mode editable-textarea overlay further down. `groups` is
// an array of {snippets, className} so more than one kind of highlight (a
// low-confidence flag, a "currently being read aloud" segment) can render
// at once with different colours. Longest snippets are matched first so
// one snippet that happens to contain a shorter one (e.g. a paragraph
// containing an already-flagged sentence) doesn't get split into a
// smaller, misleading highlight.
function renderHighlightedMarkdown(text, groups) {
  const classByText = new Map();
  (groups || []).forEach((g) => {
    (g.snippets || []).filter(Boolean).forEach((s) => {
      if (!classByText.has(s)) classByText.set(s, g.className || "");
    });
  });
  if (!classByText.size) return escapeHtml(text);

  const escapedForRegex = Array.from(classByText.keys())
    .sort((a, b) => b.length - a.length)
    .map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(escapedForRegex.join("|"), "g");
  let html = "";
  let lastIndex = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    html += escapeHtml(text.slice(lastIndex, match.index));
    const cls = classByText.get(match[0]) || "";
    html += `<mark${cls ? ` class="${cls}"` : ""}>${escapeHtml(match[0])}</mark>`;
    lastIndex = match.index + match[0].length;
  }
  html += escapeHtml(text.slice(lastIndex));
  return html;
}

// Same as renderHighlightedMarkdown, but also renders embedded
// ![alt](/images/<hash>.jpg) links (see convert.py) as an actual <img>
// instead of raw markdown text - only safe for read-only display, since it
// changes the rendered layout and would break the review-mode overlay's
// need to mirror the textarea's text exactly (see updateReviewHighlight).
// Also matches the old inline base64 data URI format, in case the database
// still has rows saved before that changed.
const EMBEDDED_IMAGE_LINK_RE = /!\[([^\]]*)\]\((\/images\/[^)]+|data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)\)/g;

function renderMarkdownWithImages(text, groups) {
  const re = new RegExp(EMBEDDED_IMAGE_LINK_RE);
  let html = "";
  let lastIndex = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    html += renderHighlightedMarkdown(text.slice(lastIndex, match.index), groups);
    html += `<img class="embedded-page-image" alt="${escapeHtml(match[1])}" src="${match[2]}">`;
    lastIndex = match.index + match[0].length;
  }
  html += renderHighlightedMarkdown(text.slice(lastIndex), groups);
  return html;
}

function renderChunks(container, chunks) {
  container.innerHTML = "";
  chunks.forEach((chunk) => {
    const card = document.createElement("div");
    card.className = "chunk-card" + (chunk.needs_review ? " needs-review" : "");
    const confidenceText = chunk.confidence === null || chunk.confidence === undefined
      ? "n/a" : `${chunk.confidence}%`;
    const body = renderMarkdownWithImages(chunk.markdown, [{ snippets: chunk.flagged_snippets }]);
    card.innerHTML =
      `<div class="chunk-meta">` +
      `<span>Page ${chunk.page_number}</span>` +
      `<span>Confidence: ${confidenceText}</span>` +
      (chunk.needs_review ? `<span class="badge">Needs review</span>` : "") +
      `</div>` +
      `<pre class="markdown-preview">${body}</pre>`;
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
const reviewMarkdownHighlight = document.getElementById("review-markdown-highlight");
const reviewHighlightHint = document.getElementById("review-highlight-hint");
const reviewApproveBtn = document.getElementById("review-approve-btn");
const reviewSkipBtn = document.getElementById("review-skip-btn");
const reviewRetryBtn = document.getElementById("review-retry-btn");
const reviewCancelBtn = document.getElementById("review-cancel-btn");
const reviewStatus = document.getElementById("review-status");

let reviewSessionId = null;
let reviewTotalPages = 0;
let reviewFlaggedSnippets = [];

// Re-renders the highlight overlay from the textarea's *current* value, so
// editing a flagged line makes its highlight disappear the moment the fix
// no longer matches the original flagged text - a natural "you fixed it"
// signal without any extra bookkeeping. Also shows which paragraph "Read
// aloud" (below) is currently speaking, in a different colour.
function updateReviewHighlight() {
  const text = reviewMarkdown.value;
  const groups = [
    { snippets: reviewSpeakingSegment ? [reviewSpeakingSegment] : [], className: "speaking" },
    { snippets: reviewFlaggedSnippets, className: "flagged" },
  ];
  reviewMarkdownHighlight.innerHTML = renderHighlightedMarkdown(text, groups) + "\n";
  const stillFlagged = reviewFlaggedSnippets.filter((s) => s && text.includes(s)).length;
  reviewHighlightHint.textContent = stillFlagged
    ? `${stillFlagged} highlighted section(s) below have lower-confidence or untranscribed text - check those first.`
    : "";
}
reviewMarkdown.addEventListener("input", updateReviewHighlight);
reviewMarkdown.addEventListener("scroll", () => {
  reviewMarkdownHighlight.scrollTop = reviewMarkdown.scrollTop;
  reviewMarkdownHighlight.scrollLeft = reviewMarkdown.scrollLeft;
});

// --- Read aloud: speaks the converted text paragraph by paragraph (the
// browser's own text-to-speech, no API/cost involved), highlighting each
// paragraph in the overlay above as it's spoken. Embedded images (see
// convert.py) are skipped entirely - reading out a wall of base64 would be
// both useless and slow. Paragraph, not word-level, granularity: the Web
// Speech API's word-boundary events are unreliable across browsers and
// especially for non-Latin scripts like Thai, which this app's OCR output
// often contains - a whole paragraph highlighted at a time is a much more
// robust "follow along" signal than a jittery per-word one. ---
const reviewReadAloudBtn = document.getElementById("review-read-aloud-btn");
const _EMBEDDED_IMAGE_LINE_RE = /^!\[[^\]]*\]\((\/images\/[^)]+|data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)\)$/;

let reviewSpeakingSegment = null;
let ttsSegments = [];
let ttsIndex = -1;
let ttsSpeaking = false;

function speakNextSegment() {
  ttsIndex++;
  if (!ttsSpeaking || ttsIndex >= ttsSegments.length) {
    stopReadAloud();
    return;
  }
  reviewSpeakingSegment = ttsSegments[ttsIndex];
  updateReviewHighlight();
  const utterance = new SpeechSynthesisUtterance(reviewSpeakingSegment);
  utterance.onend = speakNextSegment;
  utterance.onerror = speakNextSegment;
  window.speechSynthesis.speak(utterance);
}

function startReadAloud() {
  if (!("speechSynthesis" in window)) {
    reviewStatus.textContent = "Text-to-speech isn't supported in this browser.";
    return;
  }
  ttsSegments = reviewMarkdown.value
    .split("\n\n")
    .map((seg) => seg.trim())
    .filter((seg) => seg && !_EMBEDDED_IMAGE_LINE_RE.test(seg));
  if (!ttsSegments.length) {
    reviewStatus.textContent = "Nothing readable on this page (only image content).";
    return;
  }
  ttsIndex = -1;
  ttsSpeaking = true;
  reviewReadAloudBtn.textContent = "⏸ Stop reading";
  speakNextSegment();
}

function stopReadAloud() {
  ttsSpeaking = false;
  reviewSpeakingSegment = null;
  window.speechSynthesis.cancel();
  reviewReadAloudBtn.textContent = "🔊 Read aloud";
  updateReviewHighlight();
}

reviewReadAloudBtn.addEventListener("click", () => {
  if (ttsSpeaking) stopReadAloud();
  else startReadAloud();
});

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
  document.getElementById("upload-chunks").innerHTML = "";
  renderConsole([]);
  setReviewControlsEnabled(false);
  try {
    const res = await fetch("/api/review/start", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to start review");
    reviewSessionId = data.session_id;
    reviewTotalPages = data.total_pages;
    status.textContent = "";
    renderConsole(data.log);
    showReviewPage(data.page);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    setReviewControlsEnabled(true);
  }
}

function showReviewPage(page) {
  stopReadAloud();  // a new page replaced whatever was being read - don't keep reading the old one
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
  reviewFlaggedSnippets = page.flagged_snippets || [];
  updateReviewHighlight();
  reviewStatus.textContent = "";
}

function endReviewSession(message) {
  stopReadAloud();
  reviewPanel.classList.add("hidden");
  reviewSessionId = null;
  setReviewControlsEnabled(true);
  document.getElementById("upload-status").textContent = message;
  loadBooks();
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
    renderConsole(data.log);
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
    renderConsole(data.log);
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

// --- Browse all books: every uploaded source file, grouped from its chunks
// (see db.list_books()'s docstring for how "one book" is identified). ---
const booksListEl = document.getElementById("books-list");

async function loadBooks() {
  booksListEl.textContent = "Loading…";
  try {
    const res = await fetch("/api/books");
    const books = await res.json();
    booksListEl.innerHTML = "";
    if (!books.length) {
      booksListEl.innerHTML = `<p class="hint">No books uploaded yet.</p>`;
      return;
    }
    books.forEach((book) => booksListEl.appendChild(renderBookCard(book)));
  } catch (err) {
    booksListEl.innerHTML = `<p class="status">Error: ${err.message}</p>`;
  }
}

function renderBookCard(book) {
  const card = document.createElement("div");
  card.className = "book-card" + (book.needs_review_count ? " needs-review" : "");
  const confidenceText = book.avg_confidence === null || book.avg_confidence === undefined
    ? "n/a" : `${Math.round(book.avg_confidence)}%`;
  const pagesText = book.pages_stored === book.total_pages
    ? `${book.total_pages} page(s)`
    : `${book.pages_stored}/${book.total_pages} page(s) stored`;

  card.innerHTML =
    `<div class="chunk-meta">` +
    `<strong>${escapeHtml(book.source_filename)}</strong>` +
    `<span>${escapeHtml(book.source_type)}</span>` +
    `<span>${pagesText}</span>` +
    `<span>${escapeHtml(book.category)}</span>` +
    `<span>Confidence: ${confidenceText}</span>` +
    (book.needs_review_count
      ? `<span class="badge">${book.needs_review_count} page(s) need review</span>` : "") +
    `</div>` +
    `<div class="row">` +
    `<button class="ghost view-book-btn" type="button">View pages</button>` +
    `<a class="ghost" href="/api/books/${book.id}/export" download>Export .md</a>` +
    `</div>` +
    `<div class="book-chunks hidden"></div>`;

  const viewBtn = card.querySelector(".view-book-btn");
  const chunksEl = card.querySelector(".book-chunks");
  viewBtn.addEventListener("click", async () => {
    if (!chunksEl.classList.contains("hidden")) {
      chunksEl.classList.add("hidden");
      viewBtn.textContent = "View pages";
      return;
    }
    viewBtn.textContent = "Loading…";
    try {
      const res = await fetch(`/api/books/${book.id}`);
      const data = await res.json();
      renderChunks(chunksEl, data.chunks);
      chunksEl.classList.remove("hidden");
      viewBtn.textContent = "Hide pages";
    } catch (err) {
      chunksEl.innerHTML = `<p class="status">Error: ${err.message}</p>`;
      chunksEl.classList.remove("hidden");
      viewBtn.textContent = "View pages";
    }
  });

  return card;
}

document.getElementById("refresh-books-btn").addEventListener("click", loadBooks);
loadBooks();

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
