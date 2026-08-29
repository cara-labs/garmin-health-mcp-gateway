from garmin_health_gateway.db import _alter_role_password_statement


def test_role_password_statement_escapes_password_literal() -> None:
    password = "reader'password\\with-special-characters"
    statement = _alter_role_password_statement("garmin_mcp_reader", password)

    rendered = statement.as_string()

    assert rendered.startswith('ALTER ROLE "garmin_mcp_reader" PASSWORD ')
    assert "reader''password" in rendered
    assert "$1" not in rendered
