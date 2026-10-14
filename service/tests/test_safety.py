import pytest

from urlshortener.safety import UnsafeUrl, check_destination


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path?q=1",
        "http://8.8.8.8/dns",
        "https://[2606:4700:4700::1111]/",
        "https://sub.domain.example:8443/x",
        "https://3m.example/products",  # a label may start with a digit without being an address
        "https://localhost.example/",  # 'localhost' as a sub-label of a public name is fine
    ],
)
def test_public_destinations_pass(url):
    """FR-22 NFR-22: ordinary public URLs are unaffected by the safety checks."""
    check_destination(url, own_host="sho.rt", blocked_domains=("evil.example",))


@pytest.mark.parametrize(
    "url, reason",
    [
        ("http://localhost:8080/admin", "internal host"),
        ("http://LOCALHOST./", "internal host"),
        ("http://printer.local/", "internal host"),
        ("http://db.internal/", "internal host"),
        ("http://127.0.0.1/", "not a public address"),
        ("http://10.0.0.5/", "not a public address"),
        ("http://192.168.1.1/router", "not a public address"),
        ("http://169.254.169.254/latest/meta-data/", "not a public address"),
        ("http://[::1]/", "not a public address"),
        ("http://[fd00::1]/", "not a public address"),
        ("http://0.0.0.0/", "not a public address"),
        ("http://2130706433/", "ambiguous numeric host"),  # 127.0.0.1 as one integer
        ("http://0177.0.0.1/", "ambiguous numeric host"),  # octal spelling
        ("http://0x7f.1/", "ambiguous numeric host"),  # hex spelling
    ],
)
def test_internal_destinations_are_refused(url, reason):
    """FR-22: loopback, private, link-local and disguised addresses cannot be shortened."""
    with pytest.raises(UnsafeUrl, match=reason):
        check_destination(url)


@pytest.mark.parametrize("url", ["https://user:pw@example.com/", "https://bank.example@evil.test/login"])
def test_embedded_credentials_are_refused(url):
    """FR-22: the user@host trick that disguises the real destination is rejected."""
    with pytest.raises(UnsafeUrl, match="credentials"):
        check_destination(url)


def test_links_back_to_the_service_are_refused():
    """FR-22: a short link cannot point at the shortener itself (redirect loops and chains)."""
    with pytest.raises(UnsafeUrl, match="back at this service"):
        check_destination("http://SHO.rt/abc1234", own_host="sho.rt")
    check_destination("http://sho.rt.example/abc1234", own_host="sho.rt")  # a different host


@pytest.mark.parametrize("url", ["https://evil.example/", "https://login.evil.example/x", "https://EVIL.example./"])
def test_blocked_domains_cover_subdomains(url):
    """FR-22: a blocked domain blocks its subdomains too."""
    with pytest.raises(UnsafeUrl, match="blocked"):
        check_destination(url, blocked_domains=("evil.example",))


def test_blocklist_matches_whole_labels_only():
    """FR-22: 'notevil.example' is not caught by a block on 'evil.example'."""
    check_destination("https://notevil.example/", blocked_domains=("evil.example",))
