import pytest

from urlshortener.codec import ALPHABET, generate_code, is_valid_code


def test_generated_code_has_requested_length_and_alphabet():
    """FR-1: codes are base62 strings of the configured length."""
    code = generate_code(7)
    assert len(code) == 7
    assert set(code) <= set(ALPHABET)
    assert is_valid_code(code)


def test_generated_codes_do_not_repeat_in_practice():
    """FR-1: 62^7 possibilities make accidental collisions negligible."""
    codes = {generate_code(7) for _ in range(2000)}
    assert len(codes) == 2000


def test_too_short_codes_are_refused():
    with pytest.raises(ValueError):
        generate_code(3)


@pytest.mark.parametrize("candidate", ["", "abc", "has space", "semi;colon", "../etc", "x" * 33, "café123"])
def test_malformed_codes_are_invalid(candidate):
    """NFR-2: anything that is not a plausible code is rejected before any lookup."""
    assert not is_valid_code(candidate)
