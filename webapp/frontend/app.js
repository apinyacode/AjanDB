// --- API keys: browser-local only, never sent anywhere but this page's own backend ---
const LS_ANTHROPIC_KEY = "ajandb_anthropic_key";
const LS_OPENAI_KEY = "ajandb_openai_key";
const LS_AZURE_SPEECH_KEY = "ajandb_azure_speech_key";
const LS_AZURE_SPEECH_REGION = "ajandb_azure_speech_region";
const anthropicKeyInput = document.getElementById("anthropic-key-input");
const openaiKeyInput = document.getElementById("openai-key-input");
const azureSpeechKeyInput = document.getElementById("azure-speech-key-input");
const azureSpeechRegionInput = document.getElementById("azure-speech-region-input");
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
  azureSpeechKeyInput.value = storage.getItem(LS_AZURE_SPEECH_KEY) || "";
  azureSpeechRegionInput.value = storage.getItem(LS_AZURE_SPEECH_REGION) || "";
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
persistKey(azureSpeechKeyInput, LS_AZURE_SPEECH_KEY);
persistKey(azureSpeechRegionInput, LS_AZURE_SPEECH_REGION);

document.getElementById("clear-keys-btn").addEventListener("click", () => {
  anthropicKeyInput.value = "";
  openaiKeyInput.value = "";
  azureSpeechKeyInput.value = "";
  azureSpeechRegionInput.value = "";
  if (storage) {
    storage.removeItem(LS_ANTHROPIC_KEY);
    storage.removeItem(LS_OPENAI_KEY);
    storage.removeItem(LS_AZURE_SPEECH_KEY);
    storage.removeItem(LS_AZURE_SPEECH_REGION);
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

function currentAzureKeys() {
  return {
    azure_speech_key: azureSpeechKeyInput.value.trim() || undefined,
    azure_speech_region: azureSpeechRegionInput.value.trim() || undefined,
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
const autoValidateCheckbox = document.getElementById("auto-validate-checkbox");
const enableReadAloudCheckbox = document.getElementById("enable-read-aloud-checkbox");

// Second-model validation and Read aloud only mean anything inside a
// review session, so they stay disabled (and visually inert) until review
// mode itself is turned on.
reviewModeCheckbox.addEventListener("change", () => {
  autoValidateCheckbox.disabled = !reviewModeCheckbox.checked;
  enableReadAloudCheckbox.disabled = !reviewModeCheckbox.checked;
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
// Thai spelling flag, a "currently being read aloud" segment) can render
// at once with different colours. Longest snippets are matched first so
// one snippet that happens to contain a shorter one doesn't get split into
// a smaller, misleading highlight. (Confidence/multi-signal flags used to
// be a group here too - dropped because highlighting large or near-whole-
// page spans wasn't a useful "look here" signal; the confidence % and
// "Needs review" badge above the editor still surface that a page needs
// scrutiny, without carpeting the text in colour.)
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
    const body = renderMarkdownWithImages(chunk.markdown, []);
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
const spellcheckMenu = document.getElementById("spellcheck-menu");
const reviewApproveBtn = document.getElementById("review-approve-btn");
const reviewSkipBtn = document.getElementById("review-skip-btn");
const reviewRetryBtn = document.getElementById("review-retry-btn");
const reviewCancelBtn = document.getElementById("review-cancel-btn");
const reviewStatus = document.getElementById("review-status");
const reviewVerifyBtn = document.getElementById("review-verify-btn");
const verifyProviderSelect = document.getElementById("verify-provider-select");
const verifyStatus = document.getElementById("verify-status");
const verifyPanel = document.getElementById("verify-panel");
const verifyAgreementLabel = document.getElementById("verify-agreement-label");
const verifyDiffEl = document.getElementById("verify-diff");
const verifySuggestionHint = document.getElementById("verify-suggestion-hint");

let reviewSessionId = null;
let reviewTotalPages = 0;
let reviewTypos = [];

// Re-renders the highlight overlay from the textarea's *current* value, so
// fixing a misspelled word makes its underline disappear the moment the
// edit no longer matches the original - a natural "you fixed it" signal
// without any extra bookkeeping. Also shows which paragraph "Read aloud"
// (below) is currently speaking, in a different colour. Confidence/
// multi-signal flags are deliberately NOT a group here - see
// renderHighlightedMarkdown's comment for why.
function updateReviewHighlight() {
  const text = reviewMarkdown.value;
  const stillPresentTypos = reviewTypos.filter((t) => text.includes(t.word));
  const groups = [
    { snippets: reviewSpeakingSegment ? [reviewSpeakingSegment] : [], className: "speaking" },
    { snippets: stillPresentTypos.map((t) => t.word), className: "typo" },
  ];
  reviewMarkdownHighlight.innerHTML = renderHighlightedMarkdown(text, groups) + "\n";
  hideSpellcheckMenu();  // stale menu could point at a word/position that no longer matches
}
reviewMarkdown.addEventListener("input", updateReviewHighlight);
reviewMarkdown.addEventListener("scroll", () => {
  reviewMarkdownHighlight.scrollTop = reviewMarkdown.scrollTop;
  reviewMarkdownHighlight.scrollLeft = reviewMarkdown.scrollLeft;
  hideSpellcheckMenu();
});

// --- Inline spell-check corrections: click a red-underlined word for a
// dropdown of pythainlp's suggestions (see spellcheck.py), word-processor
// style, instead of a separate list of typos to cross-reference by hand.
// The overlay div is pointer-events:none everywhere except mark.typo
// itself (see style.css) - only clicks that land exactly on a flagged
// word are intercepted here; everything else falls through to the
// textarea underneath for normal editing. ---
function hideSpellcheckMenu() {
  spellcheckMenu.classList.add("hidden");
  spellcheckMenu.innerHTML = "";
}

function showSpellcheckMenu(markEl, typoEntry) {
  spellcheckMenu.innerHTML = "";
  const suggestions = (typoEntry.suggestions || []).slice(0, 5);
  if (!suggestions.length) {
    const empty = document.createElement("div");
    empty.className = "spellcheck-menu-empty";
    empty.textContent = "No suggestions";
    spellcheckMenu.appendChild(empty);
  } else {
    suggestions.forEach((suggestion) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = suggestion;
      btn.addEventListener("click", () => {
        // Every occurrence of this exact word, not just the one clicked -
        // an OCR/vision-LLM typo is normally the same mistake wherever it
        // recurs, and flagged_snippets-style matching is already by exact
        // string throughout this app, not by position.
        reviewMarkdown.value = reviewMarkdown.value.split(typoEntry.word).join(suggestion);
        hideSpellcheckMenu();
        updateReviewHighlight();
      });
      spellcheckMenu.appendChild(btn);
    });
  }
  const rect = markEl.getBoundingClientRect();
  spellcheckMenu.style.left = `${Math.round(rect.left)}px`;
  spellcheckMenu.style.top = `${Math.round(rect.bottom + 4)}px`;
  spellcheckMenu.classList.remove("hidden");
}

reviewMarkdownHighlight.addEventListener("click", (e) => {
  const markEl = e.target.closest("mark.typo");
  if (!markEl) return;
  const typoEntry = reviewTypos.find((t) => t.word === markEl.textContent);
  if (typoEntry) showSpellcheckMenu(markEl, typoEntry);
});
document.addEventListener("click", (e) => {
  if (!spellcheckMenu.classList.contains("hidden") &&
      !spellcheckMenu.contains(e.target) && !e.target.closest("mark.typo")) {
    hideSpellcheckMenu();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideSpellcheckMenu();
});

// --- Read aloud: speaks the converted text paragraph by paragraph via the
// backend's /api/tts endpoint (server-side Azure Speech - see tts.py),
// highlighting each paragraph in the overlay above as it's spoken. Server-
// side rather than the browser's own speechSynthesis: native voice
// availability/quality varies wildly by OS/browser, especially for Thai,
// so this way every tester hears the same voice. Embedded images (see
// convert.py) are skipped entirely - there's nothing to read there.
// Paragraph, not word-level, granularity: reliable word-boundary timing
// from streamed audio would need a whole different (word-timestamp) API
// response; a whole paragraph highlighted at a time is a simple, robust
// "follow along" signal without that complexity. ---
const reviewReadAloudBtn = document.getElementById("review-read-aloud-btn");
const _EMBEDDED_IMAGE_LINE_RE = /^!\[[^\]]*\]\((\/images\/[^)]+|data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)\)$/;

let reviewSpeakingSegment = null;
let ttsSegments = [];
let ttsIndex = -1;
let ttsSpeaking = false;
let reviewAudioEl = null;

async function speakNextSegment() {
  ttsIndex++;
  if (!ttsSpeaking || ttsIndex >= ttsSegments.length) {
    stopReadAloud();
    return;
  }
  reviewSpeakingSegment = ttsSegments[ttsIndex];
  updateReviewHighlight();
  try {
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: reviewSpeakingSegment, ...currentAzureKeys() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Text-to-speech request failed");
    if (!ttsSpeaking) return;  // stopped while that request was in flight
    reviewAudioEl = new Audio(data.audio_url);
    reviewAudioEl.onended = speakNextSegment;
    reviewAudioEl.onerror = speakNextSegment;
    await reviewAudioEl.play();
  } catch (err) {
    reviewStatus.textContent = `Read aloud error: ${err.message}`;
    stopReadAloud();
  }
}

function startReadAloud() {
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
  if (reviewAudioEl) {
    reviewAudioEl.pause();
    reviewAudioEl = null;
  }
  reviewReadAloudBtn.textContent = "🔊 Read aloud";
  updateReviewHighlight();
}

reviewReadAloudBtn.addEventListener("click", () => {
  if (ttsSpeaking) stopReadAloud();
  else startReadAloud();
});

// --- Verify with a second model: cross-checks the pending page against a
// vision-LLM call independent of whatever engine/provider produced it, and
// shows a word-level diff. Neither engine's own confidence score is fully
// trustworthy alone (a vision-LLM's is just the model guessing how sure it
// is) - agreement between two independently-run models is a much stronger
// signal, and the diff points at exactly which words to check if they
// don't agree. Costs a real API call, so this is opt-in, not automatic -
// but the page's own confidence/needs_review flags are exactly the case
// this is most worth doing for, so that's called out with a hint. ---
function renderWordDiff(segments) {
  return segments.map((seg) => {
    if (seg.tag === "equal") return escapeHtml(seg.text);
    let html = "";
    if (seg.a) html += `<span class="diff-del">${escapeHtml(seg.a)}</span>`;
    if (seg.b) html += `<span class="diff-ins">${escapeHtml(seg.b)}</span>`;
    return html;
  }).join("");
}

reviewVerifyBtn.addEventListener("click", async () => {
  if (!reviewSessionId) return;
  const provider = verifyProviderSelect.value;
  const providerLabel = provider === "anthropic" ? "Claude" : "GPT-4o";
  verifyStatus.textContent = `Asking ${providerLabel} for a second opinion…`;
  verifyPanel.classList.add("hidden");
  reviewVerifyBtn.disabled = true;
  try {
    const res = await fetch(`/api/review/${reviewSessionId}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, ...currentApiKeys() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Verification failed");
    renderConsole(data.log);
    verifyAgreementLabel.textContent =
      `${data.agreement_ratio}% word-level agreement with ${providerLabel}`;
    verifyDiffEl.innerHTML = renderWordDiff(data.diff);
    verifyPanel.classList.remove("hidden");
    verifyStatus.textContent = "";
  } catch (err) {
    verifyStatus.textContent = `Error: ${err.message}`;
  } finally {
    reviewVerifyBtn.disabled = false;
  }
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
  formData.append("auto_validate", autoValidateCheckbox.checked);
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

    // Default "verify with second model" to whichever provider *wasn't*
    // used for this conversion, so one click gets a genuinely independent
    // second opinion instead of just re-asking the same model.
    const originalProvider = engine === "vision" ? uploadProviderSelect.value : "anthropic";
    verifyProviderSelect.value = originalProvider === "anthropic" ? "openai" : "anthropic";

    showReviewPage(data.page);
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    setReviewControlsEnabled(true);
  }
}

function showReviewPage(page) {
  stopReadAloud();  // a new page replaced whatever was being read - don't keep reading the old one
  reviewPanel.classList.remove("hidden");
  reviewReadAloudBtn.classList.toggle("hidden", !enableReadAloudCheckbox.checked);
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
  reviewTypos = page.typos || [];
  updateReviewHighlight();
  reviewStatus.textContent = "";

  verifyPanel.classList.add("hidden");
  verifyStatus.textContent = "";
  verifySuggestionHint.classList.toggle("hidden", !page.needs_review);
}

function endReviewSession(message) {
  stopReadAloud();
  reviewPanel.classList.add("hidden");
  verifyPanel.classList.add("hidden");
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
