from trader.core.redact import redact_text


def test_replaces_each_secret_with_its_name() -> None:
    secrets = {"KEY": "abcd1234", "TOKEN": "zzzz9999"}
    assert redact_text("key=abcd1234 tok=zzzz9999", secrets) == "key=***KEY*** tok=***TOKEN***"


def test_longest_secret_replaced_first() -> None:
    secrets = {"SHORT": "abcd", "LONG": "abcd-efgh"}
    assert redact_text("x abcd-efgh y abcd", secrets) == "x ***LONG*** y ***SHORT***"


def test_values_too_short_to_be_credentials_are_ignored() -> None:
    assert redact_text("a b c", {"TINY": "b"}) == "a b c"
