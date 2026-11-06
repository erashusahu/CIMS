from app.core.security import hash_password, hash_refresh_token, verify_password
from app.services.audit import sanitize


def test_argon2_password_round_trip_and_whitespace_is_significant():
    password = "  correct horse battery staple  "
    digest = hash_password(password)
    assert digest.startswith("$argon2")
    assert verify_password(password, digest)
    assert not verify_password(password.strip(), digest)


def test_refresh_token_hash_is_one_way_and_stable():
    token = "opaque-refresh-secret"
    assert hash_refresh_token(token) == hash_refresh_token(token)
    assert hash_refresh_token(token) != token


def test_audit_sanitizer_removes_secrets_recursively():
    clean = sanitize({"before": {"name": "A", "password_hash": "secret"}, "refresh_token": "secret", "safe": [1]})
    assert clean == {"before": {"name": "A"}, "safe": [1]}
