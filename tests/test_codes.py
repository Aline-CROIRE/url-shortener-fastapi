from app.utils.codes import ALPHABET, generate_code


def test_code_has_7_characters() -> None:
    assert len(generate_code()) == 7


def test_code_uses_only_letters_and_digits() -> None:
    for _ in range(100):
        assert set(generate_code()) <= set(ALPHABET)
