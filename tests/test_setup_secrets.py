import shutil
import stat
import subprocess
from pathlib import Path


def script(tmp_path):
    target = tmp_path / "scripts" / "setup-secrets.sh"
    target.parent.mkdir()
    shutil.copy2(Path(__file__).parents[1] / "scripts" / "setup-secrets.sh", target)
    return target


def test_fresh_secret_setup_and_permissions(tmp_path):
    target = script(tmp_path)
    subprocess.run(
        ["bash", str(target)],
        input="test@example.invalid\nfake-password\nfake-key\nfake-db\nfake-reader\nfake-feedback\n",
        text=True,
        capture_output=True,
        check=True,
    )
    secrets = tmp_path / "secrets"
    assert len(list(secrets.glob("*.txt"))) == 6
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in secrets.glob("*.txt"))
    assert (secrets / "mcp_feedback_password.txt").stat().st_size > 0


def test_feedback_only_preserves_all_existing_credentials_and_retry(tmp_path):
    target = script(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    names = [
        "garmin_email",
        "garmin_password",
        "openai_tunnel_api_key",
        "postgres_password",
        "mcp_reader_password",
    ]
    for name in names:
        (secrets / f"{name}.txt").write_text("synthetic-existing-" + name)
    subprocess.run(
        ["bash", str(target), "--feedback-only"],
        input="fake-feedback-password\n",
        text=True,
        capture_output=True,
        check=True,
    )
    original = (secrets / "mcp_feedback_password.txt").read_bytes()
    subprocess.run(
        ["bash", str(target), "--feedback-only"],
        input="",
        text=True,
        capture_output=True,
        check=True,
    )
    assert (secrets / "mcp_feedback_password.txt").read_bytes() == original
    for name in names:
        assert (secrets / f"{name}.txt").read_text() == "synthetic-existing-" + name


def test_secret_setup_rejects_unknown_mode(tmp_path):
    target = script(tmp_path)
    result = subprocess.run(
        ["bash", str(target), "--invalid"], input="", text=True, capture_output=True
    )
    assert result.returncode == 2
    assert not (tmp_path / "secrets").exists()
