from app.shared.validators import validate_url


def test_valid_url():
    assert validate_url("https://www.example.com")  # Valid URL


def test_invalid_url():
    assert not validate_url("htp://invalid-url")  # Invalid URL


def test_url_with_ipv4():
    assert not validate_url("http://192.168.1.1")  # URL with IPv4 address


def test_url_with_ipv6():
    assert not validate_url("http://[2001:db8::1]")  # URL with IPv6 address


def test_url_with_spoo_me():
    assert not validate_url("https://www.spoo.me")  # URL containing "spoo.me"


def test_valid_url_with_query_params():
    assert validate_url(
        "https://www.example.com?param=value"
    )  # Valid URL with query parameters


def test_valid_url_with_path():
    assert validate_url(
        "https://www.example.com/path/to/resource"
    )  # Valid URL with path


def test_valid_url_with_subdomain():
    assert validate_url("https://sub.example.com")  # Valid URL with subdomain


def test_valid_url_with_port():
    assert validate_url("https://www.example.com:8080")  # Valid URL with port


def test_valid_url_with_fragment():
    assert validate_url("https://www.example.com#section")  # Valid URL with fragment


def test_valid_url_with_multiple_query_params():
    assert validate_url(
        "https://www.example.com?param1=value1&param2=value2"
    )  # Valid URL with multiple query parameters


def test_valid_url_with_https():
    assert validate_url("https://secure.example.com")  # Valid HTTPS URL


def test_valid_url_with_http():
    assert validate_url("http://www.example.com")  # Valid HTTP URL


def test_valid_url_with_long_path():
    assert validate_url(
        "https://www.example.com/a/very/long/path/to/resource"
    )  # Valid URL with a long path


def test_valid_url_with_encoded_characters():
    assert validate_url(
        "https://www.example.com/path%20with%20spaces"
    )  # Valid URL with encoded characters


def test_valid_url_with_user_info():
    assert validate_url("https://user:pass@www.example.com")  # Valid URL with user info
