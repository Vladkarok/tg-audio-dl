"""Tests for src/config.py - written FIRST (TDD RED phase)."""

from pathlib import Path

import pytest
from pydantic import ValidationError


class TestConfigLoadsFromEnv:
    """test_config_loads_from_env: Settings loads with valid BOT_TOKEN."""

    def test_loads_with_required_token(self, monkeypatch):
        """Settings should load successfully when TELEGRAM_BOT_TOKEN is set."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-test-token")

        from src.config import Settings

        settings = Settings()
        assert settings.TELEGRAM_BOT_TOKEN == "123456:ABC-test-token"

    def test_token_value_is_exact(self, monkeypatch):
        """Token value should match exactly what was set in env."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "9876543:XYZ-another-token")

        from src.config import Settings

        settings = Settings()
        assert settings.TELEGRAM_BOT_TOKEN == "9876543:XYZ-another-token"


class TestConfigMissingTokenRaises:
    """test_config_missing_token_raises: ValidationError when TELEGRAM_BOT_TOKEN missing."""

    def test_raises_validation_error_without_token(self, monkeypatch):
        """Settings should raise ValidationError when TELEGRAM_BOT_TOKEN is absent."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        from src.config import Settings

        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)

        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "TELEGRAM_BOT_TOKEN" in field_names

    def test_empty_string_token_raises(self, monkeypatch):
        """Settings should raise ValidationError when TELEGRAM_BOT_TOKEN is empty string."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

        from src.config import Settings

        with pytest.raises(ValidationError):
            Settings()


class TestConfigDefaults:
    """test_config_defaults: CACHE_DIR, S3_ENABLED, MAX_FILE_SIZE_MB defaults are correct."""

    @pytest.fixture(autouse=True)
    def set_required_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")

    def test_cache_dir_default(self):
        """CACHE_DIR should default to Path('./cache')."""
        from src.config import Settings

        settings = Settings()
        assert settings.CACHE_DIR == Path("./cache")

    def test_s3_enabled_default_false(self):
        """S3_ENABLED should default to False."""
        from src.config import Settings

        settings = Settings()
        assert settings.S3_ENABLED is False

    def test_max_file_size_mb_default(self):
        """MAX_FILE_SIZE_MB should default to 2000."""
        from src.config import Settings

        settings = Settings()
        assert settings.MAX_FILE_SIZE_MB == 2000

    def test_cache_max_size_gb_default(self):
        """CACHE_MAX_SIZE_GB should default to 5.0."""
        from src.config import Settings

        settings = Settings()
        assert settings.CACHE_MAX_SIZE_GB == 5.0

    def test_allowed_user_ids_default_empty(self):
        """ALLOWED_USER_IDS should default to empty list."""
        from src.config import Settings

        settings = Settings()
        assert settings.ALLOWED_USER_IDS == []

    def test_log_level_default(self):
        """LOG_LEVEL should default to 'INFO'."""
        from src.config import Settings

        settings = Settings()
        assert settings.LOG_LEVEL == "INFO"

    def test_playlist_max_tracks_default(self):
        """PLAYLIST_MAX_TRACKS should default to 50."""
        from src.config import Settings

        settings = Settings()
        assert settings.PLAYLIST_MAX_TRACKS == 50

    def test_rate_limit_per_minute_default(self):
        """RATE_LIMIT_PER_MINUTE should default to 5."""
        from src.config import Settings

        settings = Settings()
        assert settings.RATE_LIMIT_PER_MINUTE == 5

    def test_aws_region_default(self):
        """AWS_REGION should default to 'us-east-1'."""
        from src.config import Settings

        settings = Settings()
        assert settings.AWS_REGION == "us-east-1"

    def test_telegram_local_server_url_default_none(self, monkeypatch):
        """TELEGRAM_LOCAL_SERVER_URL should default to None when not configured."""
        monkeypatch.delenv("TELEGRAM_LOCAL_SERVER_URL", raising=False)

        from src.config import Settings

        settings = Settings(_env_file=None)
        assert settings.TELEGRAM_LOCAL_SERVER_URL is None

    def test_s3_bucket_default_none(self):
        """S3_BUCKET should default to None."""
        from src.config import Settings

        settings = Settings()
        assert settings.S3_BUCKET is None

    def test_aws_access_key_default_none(self):
        """AWS_ACCESS_KEY_ID should default to None."""
        from src.config import Settings

        settings = Settings()
        assert settings.AWS_ACCESS_KEY_ID is None

    def test_aws_secret_key_default_none(self):
        """AWS_SECRET_ACCESS_KEY should default to None."""
        from src.config import Settings

        settings = Settings()
        assert settings.AWS_SECRET_ACCESS_KEY is None

    def test_chapter_pages_default_true(self):
        """Chapter pages are the default overflow rendering."""
        from src.config import Settings

        settings = Settings()
        assert settings.CHAPTER_PAGES_ENABLED is True


class TestConfigAllowedUsersParsed:
    """test_config_allowed_users_parsed: "123,456" string parses to [123, 456]."""

    @pytest.fixture(autouse=True)
    def set_required_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")

    def test_comma_separated_ids_parsed(self, monkeypatch):
        """ALLOWED_USER_IDS='123,456' should parse to [123, 456]."""
        monkeypatch.setenv("ALLOWED_USER_IDS", "123,456")

        from src.config import Settings

        settings = Settings()
        assert settings.ALLOWED_USER_IDS == [123, 456]

    def test_single_user_id_parsed(self, monkeypatch):
        """ALLOWED_USER_IDS='999' should parse to [999]."""
        monkeypatch.setenv("ALLOWED_USER_IDS", "999")

        from src.config import Settings

        settings = Settings()
        assert settings.ALLOWED_USER_IDS == [999]

    def test_three_user_ids_parsed(self, monkeypatch):
        """ALLOWED_USER_IDS='1,2,3' should parse to [1, 2, 3]."""
        monkeypatch.setenv("ALLOWED_USER_IDS", "1,2,3")

        from src.config import Settings

        settings = Settings()
        assert settings.ALLOWED_USER_IDS == [1, 2, 3]

    def test_empty_allowed_user_ids_is_empty_list(self, monkeypatch):
        """ALLOWED_USER_IDS='' should produce []."""
        monkeypatch.setenv("ALLOWED_USER_IDS", "")

        from src.config import Settings

        settings = Settings()
        assert settings.ALLOWED_USER_IDS == []

    def test_user_ids_are_integers(self, monkeypatch):
        """Parsed user IDs should be integers, not strings."""
        monkeypatch.setenv("ALLOWED_USER_IDS", "100,200")

        from src.config import Settings

        settings = Settings()
        assert all(isinstance(uid, int) for uid in settings.ALLOWED_USER_IDS)


class TestConfigOverrideFromEnv:
    """Settings values can be overridden via environment variables."""

    @pytest.fixture(autouse=True)
    def set_required_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")

    def test_cache_dir_can_be_overridden(self, monkeypatch):
        """CACHE_DIR env var should override the default."""
        monkeypatch.setenv("CACHE_DIR", "/tmp/my_cache")

        from src.config import Settings

        settings = Settings()
        assert settings.CACHE_DIR == Path("/tmp/my_cache")

    def test_s3_enabled_can_be_set_true(self, monkeypatch):
        """S3_ENABLED=true should set the flag to True."""
        monkeypatch.setenv("S3_ENABLED", "true")
        monkeypatch.setenv("S3_BUCKET", "my-bucket")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv(
            "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )

        from src.config import Settings

        settings = Settings()
        assert settings.S3_ENABLED is True

    def test_chapter_pages_can_be_disabled(self, monkeypatch):
        """CHAPTER_PAGES_ENABLED=false falls back to the legacy index."""
        monkeypatch.setenv("CHAPTER_PAGES_ENABLED", "false")

        from src.config import Settings

        settings = Settings()
        assert settings.CHAPTER_PAGES_ENABLED is False

    def test_local_server_url_can_be_set(self, monkeypatch):
        """TELEGRAM_LOCAL_SERVER_URL should accept a URL string."""
        monkeypatch.setenv("TELEGRAM_LOCAL_SERVER_URL", "http://localhost:8081")

        from src.config import Settings

        settings = Settings()
        assert settings.TELEGRAM_LOCAL_SERVER_URL == "http://localhost:8081"

    def test_max_file_size_can_be_overridden(self, monkeypatch):
        """MAX_FILE_SIZE_MB env var should override the default."""
        monkeypatch.setenv("MAX_FILE_SIZE_MB", "500")

        from src.config import Settings

        settings = Settings()
        assert settings.MAX_FILE_SIZE_MB == 500


def test_local_server_url_must_have_scheme(monkeypatch):
    """TELEGRAM_LOCAL_SERVER_URL without http/https scheme must raise."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_LOCAL_SERVER_URL", "localhost:8081")

    from src.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_max_file_size_must_be_positive(monkeypatch):
    """MAX_FILE_SIZE_MB=0 would reject every download → must raise."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "0")

    from src.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_playlist_max_tracks_must_be_positive(monkeypatch):
    """PLAYLIST_MAX_TRACKS=0 would make every playlist look empty → must raise."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("PLAYLIST_MAX_TRACKS", "0")

    from src.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_local_server_url_valid_http(monkeypatch):
    """TELEGRAM_LOCAL_SERVER_URL with http:// scheme must be accepted."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_LOCAL_SERVER_URL", "http://localhost:8081")

    from src.config import Settings

    s = Settings()
    assert s.TELEGRAM_LOCAL_SERVER_URL == "http://localhost:8081"


def test_local_server_url_valid_https(monkeypatch):
    """TELEGRAM_LOCAL_SERVER_URL with https:// scheme must be accepted."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_LOCAL_SERVER_URL", "https://bot-api.example.com")

    from src.config import Settings

    s = Settings()
    assert s.TELEGRAM_LOCAL_SERVER_URL == "https://bot-api.example.com"


def test_config_s3_enabled_without_bucket_raises(monkeypatch):
    """S3_ENABLED=true without S3_BUCKET must raise a validation error."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("S3_ENABLED", "true")
    monkeypatch.delenv("S3_BUCKET", raising=False)

    from src.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_config_s3_enabled_without_aws_keys_accepted(monkeypatch):
    """S3_ENABLED=true with S3_BUCKET but no AWS keys should succeed.

    boto3 falls back to its credential chain (IAM roles, instance profiles, etc.)
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("S3_ENABLED", "true")
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    from src.config import Settings

    s = Settings()
    assert s.S3_ENABLED is True
    assert s.S3_BUCKET == "my-bucket"
    assert s.AWS_ACCESS_KEY_ID is None


# ---------------------------------------------------------------------------
# PROXY_URL scheme validation
# ---------------------------------------------------------------------------


class TestProxyUrlValidation:
    def test_proxy_url_socks5_accepted(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "socks5://user:pass@host:1080")
        from src.config import Settings

        s = Settings()
        assert s.PROXY_URL == "socks5://user:pass@host:1080"

    def test_proxy_url_http_accepted(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "http://proxy:8080")
        from src.config import Settings

        s = Settings()
        assert s.PROXY_URL == "http://proxy:8080"

    def test_proxy_url_https_accepted(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "https://proxy:8443")
        from src.config import Settings

        s = Settings()
        assert s.PROXY_URL == "https://proxy:8443"

    def test_proxy_url_socks4_accepted(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "socks4://host:1080")
        from src.config import Settings

        s = Settings()
        assert s.PROXY_URL == "socks4://host:1080"

    def test_proxy_url_invalid_scheme_rejected(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "ftp://host:21")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_proxy_url_empty_string_becomes_none(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "")
        from src.config import Settings

        s = Settings()
        assert s.PROXY_URL is None


class TestCookiesFileValidation:
    def test_cookies_file_empty_string_becomes_none(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("COOKIES_FILE", "")
        from src.config import Settings

        s = Settings()
        assert s.COOKIES_FILE is None

    def test_cookies_file_path_is_preserved(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("COOKIES_FILE", "/app/cookies.txt")
        from src.config import Settings

        s = Settings()
        assert s.COOKIES_FILE == "/app/cookies.txt"


# ---------------------------------------------------------------------------
# Numeric field validators
# ---------------------------------------------------------------------------


class TestNumericValidators:
    @pytest.fixture(autouse=True)
    def set_required_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")

    def test_cache_max_size_gb_zero_rejected(self, monkeypatch):
        monkeypatch.setenv("CACHE_MAX_SIZE_GB", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_cache_max_size_gb_negative_rejected(self, monkeypatch):
        monkeypatch.setenv("CACHE_MAX_SIZE_GB", "-1")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_cache_max_size_gb_positive_accepted(self, monkeypatch):
        monkeypatch.setenv("CACHE_MAX_SIZE_GB", "2.5")
        from src.config import Settings

        s = Settings()
        assert s.CACHE_MAX_SIZE_GB == 2.5

    def test_rate_limit_zero_rejected(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_rate_limit_negative_rejected(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "-5")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_rate_limit_positive_accepted(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "10")
        from src.config import Settings

        s = Settings()
        assert s.RATE_LIMIT_PER_MINUTE == 10

    def test_download_timeout_default(self):
        from src.config import Settings

        s = Settings()
        assert s.DOWNLOAD_TIMEOUT_SECONDS == 1800

    def test_download_timeout_zero_rejected(self, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_TIMEOUT_SECONDS", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_download_timeout_custom_accepted(self, monkeypatch):
        monkeypatch.setenv("DOWNLOAD_TIMEOUT_SECONDS", "900")
        from src.config import Settings

        s = Settings()
        assert s.DOWNLOAD_TIMEOUT_SECONDS == 900

    def test_tmp_max_age_too_low_rejected(self, monkeypatch):
        monkeypatch.setenv("TMP_MAX_AGE_SECONDS", "10")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_tmp_max_age_valid_accepted(self, monkeypatch):
        monkeypatch.setenv("TMP_MAX_AGE_SECONDS", "7200")
        from src.config import Settings

        s = Settings()
        assert s.TMP_MAX_AGE_SECONDS == 7200

    def test_tmp_cleanup_interval_too_low_rejected(self, monkeypatch):
        monkeypatch.setenv("TMP_CLEANUP_INTERVAL_SECONDS", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_tmp_cleanup_interval_valid_accepted(self, monkeypatch):
        monkeypatch.setenv("TMP_CLEANUP_INTERVAL_SECONDS", "300")
        from src.config import Settings

        s = Settings()
        assert s.TMP_CLEANUP_INTERVAL_SECONDS == 300


class TestHeartbeatAndPoolConfig:
    """Connection-pool timeout and liveness-watchdog settings."""

    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")

    def test_defaults(self):
        from src.config import Settings

        s = Settings()
        assert s.POOL_TIMEOUT_SECONDS == 20.0
        assert s.HEARTBEAT_INTERVAL_SECONDS == 30
        assert s.HEARTBEAT_PROBE_TIMEOUT_SECONDS == 20
        assert s.HEARTBEAT_MAX_FAILURES == 3

    def test_pool_timeout_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("POOL_TIMEOUT_SECONDS", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_heartbeat_interval_too_low_rejected(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "1")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_heartbeat_probe_timeout_too_low_rejected(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_PROBE_TIMEOUT_SECONDS", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_heartbeat_max_failures_too_low_rejected(self, monkeypatch):
        monkeypatch.setenv("HEARTBEAT_MAX_FAILURES", "0")
        from src.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings()

    def test_valid_overrides_accepted(self, monkeypatch):
        monkeypatch.setenv("POOL_TIMEOUT_SECONDS", "5.5")
        monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "15")
        monkeypatch.setenv("HEARTBEAT_PROBE_TIMEOUT_SECONDS", "10")
        monkeypatch.setenv("HEARTBEAT_MAX_FAILURES", "5")
        from src.config import Settings

        s = Settings()
        assert s.POOL_TIMEOUT_SECONDS == 5.5
        assert s.HEARTBEAT_INTERVAL_SECONDS == 15
        assert s.HEARTBEAT_PROBE_TIMEOUT_SECONDS == 10
        assert s.HEARTBEAT_MAX_FAILURES == 5
