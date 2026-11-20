from __future__ import annotations

import importlib
import os
import socket
import stat
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest


TEST_KEY = "sk-unit-test-control-plane-fixture-not-a-real-key"
OTHER_TEST_KEY = "sk-another-unit-test-fixture-not-a-real-key"


@pytest.fixture
def script(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    module = importlib.import_module("configure_openai_tunnel_secret")

    def no_network(*args, **kwargs):
        raise AssertionError("Secret configuration must never connect to any network")

    monkeypatch.setattr(socket, "socket", no_network)
    return module


@pytest.fixture
def destination(tmp_path):
    return tmp_path / "private-tunnel" / "control-plane.env"


def test_stores_only_expected_env_with_private_directory_and_file(script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_uid == os.geteuid()
    assert destination.parent.stat().st_uid == os.geteuid()
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("value", [
    "", " ", "sk-", "tunnel-example-id", "mcp_tunnel_example_id", "gsmcp_not_a_control_plane_key",
    "gsoat_not_a_control_plane_key", "sk- space", "sk-leading-and-trailing ", " sk-leading",
    "sk-new\nline", "sk-carriage\rreturn", "sk-tab\tvalue", "sk-null\x00value", "sk-del\x7fvalue",
    "sk-非ASCII", "sk-dollar$value", "sk-semicolon;value", "sk-quote'value", "sk-backtick`value",
    "sk-a=b", "sk-slash/value", "sk-" + "a" * 4094, None,
])
def test_rejects_invalid_key_without_creating_any_file(script, destination, value):
    with pytest.raises(script.SecretSetupError, match="INVALID_CONTROL_PLANE_KEY"):
        script.store_control_plane_key(value, destination)
    assert not destination.parent.exists()


def test_ascii_project_key_and_non_precise_length_bounds(script):
    script.validate_key("sk-proj_fixture-With_Dashes_123")
    script.validate_key("sk-" + "a" * 4093)


def test_existing_secret_requires_explicit_overwrite_and_does_not_touch_other_env(script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    unrelated = destination.parent / "unrelated.env"
    unrelated.write_text("DO_NOT_MODIFY=yes\n")
    with pytest.raises(script.SecretSetupError, match="OVERWRITE_CONFIRMATION_REQUIRED"):
        script.store_control_plane_key(OTHER_TEST_KEY, destination)
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"
    script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True)
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={OTHER_TEST_KEY}\n"
    assert unrelated.read_text() == "DO_NOT_MODIFY=yes\n"
    assert sorted(path.name for path in destination.parent.iterdir()) == ["control-plane.env", "unrelated.env"]


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777])
def test_rejects_existing_directory_with_unsafe_permissions(script, destination, mode):
    destination.parent.mkdir(mode=0o700)
    destination.parent.chmod(mode)
    with pytest.raises(script.SecretSetupError, match="UNSAFE_SECRET_DIRECTORY"):
        script.store_control_plane_key(TEST_KEY, destination)
    assert not destination.exists()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700])
def test_rejects_existing_secret_with_unsafe_permissions(script, destination, mode):
    script.store_control_plane_key(TEST_KEY, destination)
    destination.chmod(mode)
    with pytest.raises(script.SecretSetupError, match="UNSAFE_EXISTING_SECRET_FILE"):
        script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True)
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"


def test_rejects_wrong_owner(script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    with pytest.raises(script.SecretSetupError, match="UNSAFE_(SECRET_DIRECTORY|PARENT_DIRECTORY)"):
        script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True, expected_uid=os.geteuid() + 10000)
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"


def test_rejects_symlink_parent_without_modifying_destination(script, tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises((OSError, script.SecretSetupError)):
        script.store_control_plane_key(TEST_KEY, link / "control-plane.env")
    assert list(real.iterdir()) == []


def test_rejects_symlink_target_without_reading_or_modifying_it(script, destination, tmp_path):
    destination.parent.mkdir(mode=0o700)
    outside = tmp_path / "unrelated.env"
    outside.write_text("UNRELATED=yes\n")
    destination.symlink_to(outside)
    with pytest.raises(script.SecretSetupError, match="UNSAFE_EXISTING_SECRET_FILE"):
        script.store_control_plane_key(TEST_KEY, destination, overwrite=True)
    assert destination.is_symlink()
    assert outside.read_text() == "UNRELATED=yes\n"


def test_rejects_hard_linked_existing_file(script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    linked = destination.parent / "copied-link.env"
    os.link(destination, linked)
    with pytest.raises(script.SecretSetupError, match="UNSAFE_EXISTING_SECRET_FILE"):
        script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True)
    assert linked.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"


@pytest.mark.parametrize("path", [Path("relative/control-plane.env"), Path("/control-plane.env"),
                                  Path("/tmp/../tmp/control-plane.env"), Path("/tmp/another.env")])
def test_rejects_ambiguous_or_other_env_target(script, path):
    with pytest.raises(script.SecretSetupError, match="UNSAFE_DESTINATION"):
        script.store_control_plane_key(TEST_KEY, path)


def test_fsync_precedes_atomic_replace_and_directory_sync_follows(monkeypatch, script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    events = []
    original_fsync = os.fsync
    original_replace = os.replace

    def sync(descriptor):
        info = os.fstat(descriptor)
        events.append("file_sync" if stat.S_ISREG(info.st_mode) else "directory_sync")
        if stat.S_ISREG(info.st_mode):
            assert stat.S_IMODE(info.st_mode) == 0o600
        return original_fsync(descriptor)

    def replace(source, target, **kwargs):
        assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"
        events.append("replace")
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(script.os, "fsync", sync)
    monkeypatch.setattr(script.os, "replace", replace)
    script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True)
    assert events == ["file_sync", "replace", "directory_sync"]
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={OTHER_TEST_KEY}\n"


def test_write_failure_preserves_existing_secret_and_cleans_temporary_file(monkeypatch, script, destination):
    script.store_control_plane_key(TEST_KEY, destination)

    def fail_write(*args):
        raise OSError("PRIVATE-ERROR-TEXT")

    monkeypatch.setattr(script.os, "write", fail_write)
    with pytest.raises(OSError):
        script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True)
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"
    assert list(destination.parent.iterdir()) == [destination]


def test_metadata_check_never_opens_existing_secret(monkeypatch, script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        assert Path(path).name != "control-plane.env", "Existing secret content must never be opened"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(script.os, "open", guarded_open)
    assert script.destination_exists(destination) is True
    script.store_control_plane_key(OTHER_TEST_KEY, destination, overwrite=True)


def test_interactive_double_hidden_entry_stores_key_and_ignores_env(monkeypatch, script, destination, capsys):
    prompts = []
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", OTHER_TEST_KEY)

    def read_secret(prompt):
        prompts.append(prompt)
        return TEST_KEY

    assert script.configure_secret(destination, input_stream=SimpleNamespace(isatty=lambda: True), secret_reader=read_secret)
    assert len(prompts) == 2
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"
    output = capsys.readouterr()
    assert TEST_KEY not in output.out + output.err and OTHER_TEST_KEY not in output.out + output.err


def test_non_tty_fails_before_hidden_input_or_file_changes(script, destination):
    def forbidden_prompt(prompt):
        raise AssertionError("Must reject non-TTY input before asking for a key")

    with pytest.raises(script.SecretSetupError, match="INTERACTIVE_TTY_REQUIRED"):
        script.configure_secret(destination, input_stream=SimpleNamespace(isatty=lambda: False), secret_reader=forbidden_prompt)
    assert not destination.parent.exists()


def test_getpass_echo_warning_fails_closed(script, destination):
    def warning_prompt(prompt):
        warnings.warn("Echo must not be allowed", script.getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("GetPassWarning must stop the fallback")

    with pytest.raises(script.SecretSetupError, match="HIDDEN_INPUT_UNAVAILABLE"):
        script.configure_secret(destination, input_stream=SimpleNamespace(isatty=lambda: True), secret_reader=warning_prompt)
    assert not destination.parent.exists()


def test_confirmation_mismatch_does_not_create_file(script, destination):
    answers = iter([TEST_KEY, OTHER_TEST_KEY])
    with pytest.raises(script.SecretSetupError, match="KEY_CONFIRMATION_MISMATCH"):
        script.configure_secret(destination, input_stream=SimpleNamespace(isatty=lambda: True), secret_reader=lambda prompt: next(answers))
    assert not destination.parent.exists()


@pytest.mark.parametrize("answer", ["", "yes", "overwrite", "OVERWRITE "])
def test_overwrite_cancellation_preserves_original_without_reading_key(script, destination, answer):
    script.store_control_plane_key(TEST_KEY, destination)

    def forbidden_prompt(prompt):
        raise AssertionError("Cancelled overwrite must not ask for a key")

    assert script.configure_secret(destination, input_stream=SimpleNamespace(isatty=lambda: True),
                                   secret_reader=forbidden_prompt, confirm_reader=lambda prompt: answer) is False
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={TEST_KEY}\n"


def test_explicit_overwrite_confirmation_allows_replacement(script, destination):
    script.store_control_plane_key(TEST_KEY, destination)
    assert script.configure_secret(destination, input_stream=SimpleNamespace(isatty=lambda: True),
                                   secret_reader=lambda prompt: OTHER_TEST_KEY, confirm_reader=lambda prompt: "OVERWRITE")
    assert destination.read_text() == f"CONTROL_PLANE_API_KEY={OTHER_TEST_KEY}\n"


def test_cli_rejects_key_arguments_without_echoing_them(monkeypatch, script, capsys):
    monkeypatch.setattr(sys, "argv", ["configure-secret.py", "--key", TEST_KEY])
    assert script.main() == 1
    output = capsys.readouterr()
    assert "COMMAND_LINE_ARGUMENTS_NOT_ALLOWED" in output.err
    assert TEST_KEY not in output.out + output.err


def test_cli_requires_root(monkeypatch, script, capsys):
    monkeypatch.setattr(sys, "argv", ["configure-secret.py"])
    monkeypatch.setattr(script.os, "geteuid", lambda: 10000)
    assert script.main() == 1
    assert "ROOT_REQUIRED" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [OSError("PRIVATE-ERROR-DETAILS"), EOFError(), KeyboardInterrupt()])
def test_cli_error_output_is_fixed_and_does_not_disclose_input(monkeypatch, script, capsys, failure):
    monkeypatch.setattr(sys, "argv", ["configure-secret.py"])
    monkeypatch.setattr(script.os, "geteuid", lambda: 0)

    def fail(**kwargs):
        raise failure

    monkeypatch.setattr(script, "configure_secret", fail)
    assert script.main() == 1
    output = capsys.readouterr()
    assert "PRIVATE-ERROR-DETAILS" not in output.out + output.err
    assert "Traceback" not in output.out + output.err


def test_cli_success_does_not_report_key_attributes_or_start_services(monkeypatch, script, capsys):
    monkeypatch.setattr(sys, "argv", ["configure-secret.py"])
    monkeypatch.setattr(script.os, "geteuid", lambda: 0)

    def configured(**kwargs):
        assert kwargs == {"expected_uid": 0}
        return True

    monkeypatch.setattr(script, "configure_secret", configured)
    assert script.main() == 0
    output = capsys.readouterr()
    assert "已安全保存" in output.out and "未启动或重启" in output.out
    assert TEST_KEY not in output.out + output.err and output.err == ""
