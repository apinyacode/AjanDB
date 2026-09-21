from backend import ensemble_verify


# --- word_diff (moved here from review.py - see review.py's docstring) ---

def test_word_diff_reports_full_agreement_for_identical_text():
    diff = ensemble_verify.word_diff("Hello world", "Hello world")
    assert diff["agreement_ratio"] == 100.0
    assert diff["segments"] == [{"tag": "equal", "text": "Hello world"}]


def test_word_diff_flags_a_single_changed_word_as_one_segment():
    diff = ensemble_verify.word_diff("The cat sat", "The dog sat")
    assert diff["agreement_ratio"] < 100.0
    tags = [s["tag"] for s in diff["segments"]]
    assert "replace" in tags
    replaced = next(s for s in diff["segments"] if s["tag"] == "replace")
    assert replaced["a"] == "cat"
    assert replaced["b"] == "dog"


def test_word_diff_completely_different_text_has_low_agreement():
    diff = ensemble_verify.word_diff("Completely different content here", "Nothing at all in common")
    assert diff["agreement_ratio"] < 60.0
    assert all(s["tag"] != "equal" for s in diff["segments"] if "text" in s and s["text"].strip())


# --- convert_page_ensemble ---

def test_convert_page_ensemble_calls_each_provider_and_returns_its_markdown(monkeypatch):
    calls = []

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        calls.append((provider, api_key))
        return [{"page_number": 1, "markdown": f"text from {provider}", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(ensemble_verify.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    result = ensemble_verify.convert_page_ensemble(
        "fake.pdf", providers=("anthropic", "openai"),
        api_keys={"anthropic": "key-a", "openai": "key-b"})

    assert result == {"anthropic": "text from anthropic", "openai": "text from openai"}
    assert set(calls) == {("anthropic", "key-a"), ("openai", "key-b")}


def test_convert_page_ensemble_falls_back_to_none_key_when_not_supplied(monkeypatch):
    seen_keys = {}

    def fake_pdf_to_markdown_vision(pdf_path, provider=None, model=None, api_key=None):
        seen_keys[provider] = api_key
        return [{"page_number": 1, "markdown": "text", "confidence": 90.0,
                  "needs_review": False, "flagged_snippets": []}]

    monkeypatch.setattr(ensemble_verify.convert, "pdf_to_markdown_vision", fake_pdf_to_markdown_vision)

    ensemble_verify.convert_page_ensemble("fake.pdf", providers=("anthropic",))

    assert seen_keys == {"anthropic": None}


# --- diff_ensemble ---
# Per the task: full agreement, a single-word disagreement, a reordering,
# and empty input.

def test_diff_ensemble_full_agreement():
    result = ensemble_verify.diff_ensemble({"anthropic": "The cat sat on the mat",
                                             "openai": "The cat sat on the mat"})
    assert result["agreement_pct"] == 100.0
    assert result["spans"] == [{"text": "The cat sat on the mat", "agreement": "full"}]


def test_diff_ensemble_single_word_disagreement():
    result = ensemble_verify.diff_ensemble({"anthropic": "The cat sat on the mat",
                                             "openai": "The dog sat on the mat"})
    assert result["agreement_pct"] < 100.0
    disagreements = [s for s in result["spans"] if s["agreement"] == "none"]
    assert len(disagreements) == 1
    assert disagreements[0]["providers"] == {"anthropic": "cat", "openai": "dog"}
    # everything else still agrees
    agreements = [s for s in result["spans"] if s["agreement"] == "full"]
    assert "".join(s["text"] for s in agreements) == "The  sat on the mat"


def test_diff_ensemble_reordering_shows_up_as_disagreement():
    result = ensemble_verify.diff_ensemble({"anthropic": "apple banana cherry",
                                             "openai": "cherry banana apple"})
    assert result["agreement_pct"] < 100.0
    assert any(s["agreement"] == "none" for s in result["spans"])


def test_diff_ensemble_empty_input_returns_full_agreement_trivially():
    assert ensemble_verify.diff_ensemble({}) == {"spans": [], "agreement_pct": 100.0}


def test_diff_ensemble_single_provider_is_trivially_full_agreement():
    result = ensemble_verify.diff_ensemble({"anthropic": "some text"})
    assert result == {"spans": [{"text": "some text", "agreement": "full"}], "agreement_pct": 100.0}


def test_diff_ensemble_preserves_provider_insertion_order_in_spans():
    # review.py relies on the *first* key in `providers` being whichever
    # provider's text is what's actually shown in the editable textarea
    # (see _run_ensemble_verification) - insertion order into the `outputs`
    # dict passed here must survive into each disagreement span.
    result = ensemble_verify.diff_ensemble({"openai": "one two three", "anthropic": "one XXX three"})
    disagreement = next(s for s in result["spans"] if s["agreement"] == "none")
    assert list(disagreement["providers"].keys()) == ["openai", "anthropic"]
