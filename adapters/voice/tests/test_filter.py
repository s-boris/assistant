from voice_relay import answer_filter as af
from voice_relay.answer_filter import filter_answer


def test_uses_the_llm_local_regex_pass():
    assert af.engine() == "llm-local"


def test_masks_contact_and_bank_identifiers():
    f = filter_answer("Call 079 123 45 67 or +41 44 668 18 00, mail anna.muster@example.com, "
                      "IBAN CH93 0076 2011 6238 5295 7, AHV 756.1234.5678.97, card 4111 1111 1111 1111.")  # secretscan:allow (public test card number)
    for bad in ("079", "+41", "@example", "CH93", "756.", "4111"):
        assert bad not in f.text, (bad, f.text)
    assert "withheld" in f.text and f.masked


def test_masks_street_secret_and_long_numbers():
    f = filter_answer("Pick it up at Bahnhofstrasse 12. The door code is 4711, customer number 1234 5678 901.")
    assert "Bahnhofstrasse 12" not in f.text and "4711" not in f.text and "1234 5678 901" not in f.text


def test_amounts_masked_by_default_and_optional():
    assert "49.90" not in filter_answer("The bill is CHF 49.90, due Friday.").text
    assert "49.90" in filter_answer("The bill is CHF 49.90, due Friday.", mask_amounts=False).text


def test_keeps_times_dates_and_small_numbers():
    s = "Your train leaves at 7:42 on 20.10.2026 from platform 3; it is 12 degrees."
    assert filter_answer(s).text == s


def test_spoken_shape_markdown_links_and_length():
    f = filter_answer("**Done.** See https://example.com/x?y=1 and `code`.")
    assert "**" not in f.text and "https" not in f.text and "link on Telegram" in f.text
    long = filter_answer(" ".join(["This is a fairly long sentence about the plan."] * 30))
    assert long.truncated and long.text.endswith("More on Telegram.") and len(long.text) < 500


def test_fallback_rules_when_llm_local_is_missing(monkeypatch):
    monkeypatch.setattr(af, "_regex_pass", False)
    f = filter_answer("Call 079 123 45 67, mail a.b@example.com, IBAN CH93 0076 2011 6238 5295 7")
    assert af.engine() == "fallback"
    assert "079" not in f.text and "@example" not in f.text and "CH93" not in f.text
