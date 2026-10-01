from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

from garmin_health_gateway.config import Settings


def test_settings_read_secret_files_and_escape_database_password(tmp_path, monkeypatch) -> None:
    password = tmp_path / "postgres"
    email = tmp_path / "email"
    garmin_password = tmp_path / "garmin-password"
    password.write_text("a/b+c=", encoding="utf-8")
    email.write_text("runner@example.com\n", encoding="utf-8")
    garmin_password.write_text("secret\n", encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(password))
    monkeypatch.setenv("GARMIN_EMAIL_FILE", str(email))
    monkeypatch.setenv("GARMIN_PASSWORD_FILE", str(garmin_password))

    result = Settings.from_env()

    assert "a%2Fb%2Bc%3D" in result.database_url
    assert result.garmin_email == "runner@example.com"
    assert result.garmin_password == "secret"


def test_environment_secret_takes_precedence_over_file(tmp_path, monkeypatch) -> None:
    password = tmp_path / "postgres"
    password.write_text("wrong", encoding="utf-8")
    monkeypatch.setenv("POSTGRES_PASSWORD", "right")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(password))

    assert "right" in Settings.from_env().database_url


def test_feedback_credentials_are_separate_from_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://garmin_mcp_reader:reader@localhost/test")
    secret = tmp_path / "feedback-password"
    secret.write_text("feedback'only")
    monkeypatch.setenv("MCP_FEEDBACK_PASSWORD_FILE", str(secret))
    result = Settings.from_env()
    assert conninfo_to_dict(result.database_url)["user"] == "garmin_mcp_reader"
    assert conninfo_to_dict(result.feedback_database_url)["user"] == "garmin_mcp_feedback_writer"
    assert conninfo_to_dict(result.feedback_database_url)["password"] == "feedback'only"
