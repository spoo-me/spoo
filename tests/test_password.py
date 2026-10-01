from app.shared.validators import validate_url_password as validate_password


def test_password_too_short():
    assert not validate_password("Ab1@")  # Less than 8 characters


def test_password_no_letter():
    assert not validate_password("12345678@")  # No letters


def test_password_no_number():
    assert not validate_password("Abcdefgh@")  # No numbers


def test_password_no_special_char():
    assert not validate_password("Abcdefgh1")  # No special characters


def test_password_consecutive_special_chars():
    assert not validate_password("Abcdef1@@")  # Consecutive special characters


def test_valid_password():
    assert validate_password("Abcdef1@")  # Valid password


def test_valid_password_with_dot():
    assert validate_password("Abcdef1.")  # Valid password with dot


def test_password_with_invalid_special_char():
    assert not validate_password("Abcdef1#")
