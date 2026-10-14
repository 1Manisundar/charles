import pytest

from urlshortener.codec import ALPHABET, InvalidAlias, generate_code, is_valid_code, validate_alias


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


@pytest.mark.parametrize("candidate", ["", "ab", "has space", "semi;colon", "../etc", "x" * 33, "café123"])
def test_malformed_codes_are_invalid(candidate):
    """NFR-2: anything that is not a plausible code is rejected before any lookup."""
    assert not is_valid_code(candidate)


@pytest.mark.parametrize("alias", ["spring-sale", "Q4_2026", "abc", "a" * 32])
def test_well_formed_aliases_are_accepted(alias):
    """FR-12: letters, digits, '-' and '_' (3-32 characters) are valid aliases and valid codes."""
    assert validate_alias(alias) == alias
    assert is_valid_code(alias)


@pytest.mark.parametrize(
    "alias", ["", "ab", "a" * 33, "-leading", "_leading", "has space", "slash/inside", "dot.inside", "café-bar"]
)
def test_malformed_aliases_are_rejected(alias):
    """FR-13: aliases that could break routing or look like paths are refused."""
    with pytest.raises(InvalidAlias):
        validate_alias(alias)


@pytest.mark.parametrize("alias", ["api", "docs", "healthz", "readyz", "API", "Docs"])
def test_reserved_names_are_rejected(alias):
    """FR-13: an alias may not shadow a route the service owns, whatever its casing."""
    with pytest.raises(InvalidAlias):
        validate_alias(alias)
