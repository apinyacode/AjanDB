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
