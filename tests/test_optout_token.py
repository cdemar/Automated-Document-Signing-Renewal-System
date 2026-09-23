"""Tests for the HMAC opt-out token module.

optout_token.py has one external dependency: AWS Secrets Manager.
The autouse fixture below swaps the cached secret for a test value so
every test runs without a real AWS connection.
"""
import pytest
import optout_token


# ---------------------------------------------------------------------------
# Setup: inject a fake secret so _get_secret() never calls AWS
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def inject_secret(monkeypatch):
    monkeypatch.setattr(optout_token, "_secret", "test-secret-key")


# ---------------------------------------------------------------------------
# make_token
# ---------------------------------------------------------------------------

class TestMakeToken:
    def test_returns_hex_string(self):
        token = optout_token.make_token("person-1", "form-1")
        assert all(c in "0123456789abcdef" for c in token)

    def test_is_sha256_length(self):
        """SHA-256 hex digest is always 64 characters."""
        token = optout_token.make_token("person-1", "form-1")
        assert len(token) == 64

    def test_is_deterministic(self):
        """Same inputs must always produce the same token."""
        assert (
            optout_token.make_token("person-1", "form-1")
            == optout_token.make_token("person-1", "form-1")
        )

    @pytest.mark.parametrize("person_id,form_id", [
        ("person-2", "form-1"),
        ("person-1", "form-2"),
        ("other",    "other"),
    ])
    def test_unique_per_input(self, person_id, form_id):
        """A different person_id or form_id must produce a different token."""
        baseline = optout_token.make_token("person-1", "form-1")
        assert optout_token.make_token(person_id, form_id) != baseline


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------

class TestVerifyToken:
    def test_valid_token_passes(self):
        token = optout_token.make_token("person-1", "form-1")
        assert optout_token.verify_token("person-1", "form-1", token) is True

    @pytest.mark.parametrize("bad_token", [
        "completely-wrong",
        "a" * 64,   # right length, wrong value
        "",
        None,
    ])
    def test_bad_tokens_are_rejected(self, bad_token):
        assert optout_token.verify_token("person-1", "form-1", bad_token) is False

    def test_wrong_person_id_rejected(self):
        token = optout_token.make_token("person-1", "form-1")
        assert optout_token.verify_token("person-WRONG", "form-1", token) is False

    def test_wrong_form_id_rejected(self):
        token = optout_token.make_token("person-1", "form-1")
        assert optout_token.verify_token("person-1", "form-WRONG", token) is False

    def test_token_changes_when_secret_changes(self, monkeypatch):
        """Tokens signed under a different secret must not verify."""
        token = optout_token.make_token("person-1", "form-1")
        monkeypatch.setattr(optout_token, "_secret", "completely-different-secret")
        assert optout_token.verify_token("person-1", "form-1", token) is False


# ---------------------------------------------------------------------------
# optout_url
# ---------------------------------------------------------------------------

class TestOptoutUrl:
    def test_url_contains_person_id(self):
        url = optout_token.optout_url("https://api.example.com", "pid-1", "fid-1")
        assert "pid=pid-1" in url

    def test_url_contains_form_id(self):
        url = optout_token.optout_url("https://api.example.com", "pid-1", "fid-1")
        assert "fid=fid-1" in url

    def test_url_contains_token(self):
        url = optout_token.optout_url("https://api.example.com", "pid-1", "fid-1")
        assert "token=" in url

    def test_embedded_token_is_valid(self):
        """The token baked into the URL must verify correctly."""
        url = optout_token.optout_url("https://api.example.com", "pid-1", "fid-1")
        token = url.split("token=")[1]
        assert optout_token.verify_token("pid-1", "fid-1", token) is True

    def test_url_uses_provided_base(self):
        url = optout_token.optout_url("https://custom.gateway.com", "pid-1", "fid-1")
        assert url.startswith("https://custom.gateway.com/optout")
