from backend import spellcheck


def test_correctly_spelled_thai_text_has_no_typos():
    text = "สวัสดีครับ วันนี้อากาศดีมาก ผมไปตลาดเพื่อซื้อผลไม้"
    assert spellcheck.find_thai_typos(text) == []


def test_misspelled_thai_word_is_flagged_with_a_suggestion():
    text = "สวัสดร ครับ"
    typos = spellcheck.find_thai_typos(text)
    assert len(typos) == 1
    assert typos[0]["word"] == "สวัสดร"
    assert "สวัสดี" in typos[0]["suggestions"]


def test_non_thai_tokens_are_never_flagged():
    text = "Field Visit Notes 12345 — OCR PDF"
    assert spellcheck.find_thai_typos(text) == []


def test_duplicate_typos_are_only_reported_once():
    text = "สวัสดร สวัสดร สวัสดร"
    typos = spellcheck.find_thai_typos(text)
    assert len(typos) == 1


# --- contains_thai / tokenizer_divergence (see review.py's ensemble
# verification - a second, independent "this span looks garbled" signal
# on top of two vision-LLM providers disagreeing) ---

def test_contains_thai_detects_thai_script():
    assert spellcheck.contains_thai("สวัสดีครับ") is True


def test_contains_thai_false_for_non_thai_text():
    assert spellcheck.contains_thai("Field Visit Notes 12345") is False


def test_tokenizer_divergence_is_high_for_a_clean_correctly_spelled_sentence():
    # a normal, correctly-spelled sentence has exactly one sensible way to
    # segment it, so both tokenizers should mostly agree.
    text = "สวัสดีครับ วันนี้อากาศดีมาก"
    assert spellcheck.tokenizer_divergence(text) >= 70.0


def test_tokenizer_divergence_returns_a_percentage_in_range():
    text = "สวัสดร ครับ"  # the misspelled word from the typo test above
    divergence = spellcheck.tokenizer_divergence(text)
    assert 0.0 <= divergence <= 100.0
