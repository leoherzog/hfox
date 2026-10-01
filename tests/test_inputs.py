"""Guarded input: files and stdin read through AppContext, the config-directory guard and
the credential content check.
"""

import io
import json
import os
import sys
from pathlib import Path

import pytest

import hfox.cli._util as util_mod
from hfox.cli._util import (
    STDIN,
    check_no_secrets,
    check_readable_path,
    load_json_file,
    open_guarded,
    read_text_file,
    stdin_is_tty,
    text_file_help,
)
from hfox.cli.context import AppContext
from hfox.core.config import load_config
from hfox.core.errors import ExitCode, ValidationError

FILE_KEY = "file-api-key-0123456789"
FILE_CODE = "file-auth-code-9876543210"
ENV_KEY = "env-api-key-abcdefghij"

needs_links = pytest.mark.skipif(sys.platform == "win32", reason="links need privileges")


def feed(monkeypatch, data: bytes):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


@pytest.fixture
def cfg_dir():
    """The env-resolved config directory, holding a token.json."""
    path = Path(os.environ["HFOX_CONFIG_DIR"])
    path.mkdir(parents=True)
    token = {"subdomain": "acme", "region": "us", "api_key": FILE_KEY, "auth_code": FILE_CODE}
    (path / "token.json").write_text(json.dumps(token), encoding="utf-8")
    return path


@pytest.fixture
def ctx(cfg_dir):
    return AppContext(config=load_config())


READERS = {
    "text": lambda c, path: c.read_text(path, "--text-file"),
    "json": lambda c, path: c.read_json(path, "--file"),
    "attach": lambda c, path: c.attach({"a": 1}, [path]),
}
reader = pytest.mark.parametrize("read", READERS.values(), ids=READERS.keys())


# -- small helpers ------------------------------------------------------------
def test_stdin_constant_and_help_text():
    assert STDIN == "-"
    assert text_file_help("plain-text body") == "File holding the plain-text body; '-' reads stdin."


def test_stdin_is_tty_false_for_a_pipe_a_missing_stdin_and_a_broken_one(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert stdin_is_tty() is False
    monkeypatch.setattr(sys, "stdin", None)
    assert stdin_is_tty() is False

    class Broken:
        def isatty(self):
            raise ValueError("closed")

    monkeypatch.setattr(sys, "stdin", Broken())
    assert stdin_is_tty() is False


def test_stdin_is_tty_true_for_a_terminal(monkeypatch):
    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(sys, "platform", "linux")
    assert stdin_is_tty() is True


@pytest.mark.parametrize("console", [True, False])
def test_stdin_is_tty_on_windows_also_needs_a_console(monkeypatch, console):
    # Windows isatty() is true for NUL; only a console handle counts as a terminal.
    class Device:
        def isatty(self):
            return True

    seen = []
    monkeypatch.setattr(sys, "stdin", Device())
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(util_mod, "_has_console", lambda stream: seen.append(stream) or console)
    assert stdin_is_tty() is console
    assert seen == [sys.stdin]


def test_stdin_is_tty_on_windows_skips_the_console_check_for_a_pipe(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(util_mod, "_has_console", lambda stream: pytest.fail("checked"))
    assert stdin_is_tty() is False


def test_has_console_is_false_when_the_handle_cannot_be_checked():
    class NoHandle:
        def fileno(self):
            raise OSError("no descriptor")

    assert util_mod._has_console(NoHandle()) is False


@pytest.mark.skipif(sys.platform == "win32", reason="msvcrt exists on Windows")
def test_has_console_is_false_without_the_windows_api(tmp_path):
    with open(tmp_path / "f", "w+") as stream:
        assert util_mod._has_console(stream) is False


@pytest.mark.skipif(sys.platform != "win32", reason="needs the Windows console API")
def test_has_console_is_false_for_the_nul_device():
    with open(os.devnull) as stream:
        assert stream.isatty() is True
        assert util_mod._has_console(stream) is False


# -- reading files ------------------------------------------------------------
def test_read_text_returns_content_unmodified(ctx, tmp_path):
    f = tmp_path / "body.txt"
    f.write_bytes("  Héllo\r\nworld\n\n".encode())
    assert ctx.read_text(str(f), "--text-file") == "  Héllo\r\nworld\n\n"


def test_read_text_strips_bom(ctx, tmp_path):
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbfhi\n")
    assert ctx.read_text(str(f), "--text-file") == "hi\n"


def test_read_text_rejects_non_utf8_file(ctx, tmp_path):
    f = tmp_path / "latin.txt"
    f.write_bytes(b"ok \xe9")
    with pytest.raises(ValidationError, match=r"latin\.txt is not UTF-8 \(byte 3\)\.") as exc:
        ctx.read_text(str(f), "--text-file")
    assert exc.value.exit_code is ExitCode.VALIDATION


def test_read_text_missing_file(ctx, tmp_path):
    with pytest.raises(ValidationError, match="File not found: .*absent.txt"):
        ctx.read_text(str(tmp_path / "absent.txt"), "--text-file")


def test_read_text_directory_is_not_a_regular_file(ctx, tmp_path):
    with pytest.raises(ValidationError) as exc:
        ctx.read_text(str(tmp_path), "--text-file")
    assert str(exc.value) == f"File is not a regular file: {tmp_path}"
    assert exc.value.exit_code is ExitCode.VALIDATION


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_named_pipe_is_refused_without_being_opened(ctx, tmp_path):
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(ValidationError) as exc:
        ctx.read_text(str(pipe), "--text-file")
    assert str(exc.value) == f"File is not a regular file: {pipe}"
    with pytest.raises(ValidationError) as exc:
        ctx.attach({}, [str(pipe)])
    assert str(exc.value) == f"Attachment is not a regular file: {pipe}"


@needs_links
def test_dangling_symlink_is_not_found(ctx, tmp_path):
    link = tmp_path / "dangling.txt"
    link.symlink_to(tmp_path / "gone.txt")
    with pytest.raises(ValidationError, match="File not found: .*dangling.txt"):
        ctx.read_text(str(link), "--text-file")


def test_read_json_parses_file_and_names_it_on_error(ctx, tmp_path):
    f = tmp_path / "data.json"
    f.write_text('[{"id": 1}]', encoding="utf-8")
    assert ctx.read_json(str(f), "--file") == [{"id": 1}]
    f.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValidationError, match="Invalid JSON in .*data.json"):
        ctx.read_json(str(f), "--file")


def test_load_json_file_does_not_read_stdin(monkeypatch):
    feed(monkeypatch, b"[1]")
    with pytest.raises(ValidationError, match="File not found: -"):
        load_json_file("-")


def test_util_readers_default_to_no_guard(cfg_dir):
    assert json.loads(read_text_file(str(cfg_dir / "token.json")))["api_key"] == FILE_KEY


# -- stdin --------------------------------------------------------------------
def test_dash_reads_stdin_unmodified(ctx, monkeypatch):
    feed(monkeypatch, b"line one\nline two\n")
    assert ctx.read_text("-", "--text-file") == "line one\nline two\n"


def test_dash_strips_bom_from_stdin(ctx, monkeypatch):
    feed(monkeypatch, b"\xef\xbb\xbfhi")
    assert ctx.read_text("-", "--text-file") == "hi"


def test_dash_reads_json_from_stdin_and_names_stdin_on_error(ctx, cfg_dir, monkeypatch):
    feed(monkeypatch, b'{"a": [1, 2]}')
    assert ctx.read_json("-", "--file") == {"a": [1, 2]}
    other = AppContext(config=load_config())
    feed(monkeypatch, b"{nope")
    with pytest.raises(ValidationError, match="Invalid JSON in stdin"):
        other.read_json("-", "--file")


def test_stdin_without_a_buffer_is_read_as_text(ctx, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("plain\n"))
    assert ctx.read_text("-", "--text-file") == "plain\n"


def test_missing_stdin_reads_as_empty(ctx, monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert ctx.read_text("-", "--text-file") == ""


def test_non_utf8_stdin_rejected_naming_the_flag(ctx, monkeypatch):
    feed(monkeypatch, b"ok \xff")
    with pytest.raises(ValidationError) as exc:
        ctx.read_text("-", "--html-file")
    assert str(exc.value) == "--html-file: stdin is not UTF-8 (byte 3)."


def test_second_stdin_input_rejected_naming_both_flags(ctx, monkeypatch):
    feed(monkeypatch, b"body")
    assert ctx.read_text("-", "--text-file") == "body"
    with pytest.raises(ValidationError) as exc:
        ctx.read_text("-", "--html-file")
    assert str(exc.value) == (
        "--html-file and --text-file both read stdin; only one input may use '-'."
    )


def test_stdin_state_is_per_context(cfg_dir, monkeypatch):
    for _ in range(2):
        feed(monkeypatch, b"body")
        assert AppContext(config=load_config()).read_text("-", "--text-file") == "body"


def test_attachment_named_dash_is_an_ordinary_file(ctx, monkeypatch):
    feed(monkeypatch, b"body")
    with pytest.raises(ValidationError, match="Attachment not found: -"):
        ctx.attach({}, ["-"])
    assert ctx.read_text("-", "--text-file") == "body"


# -- text_input ---------------------------------------------------------------
def test_text_input_returns_inline_untouched(ctx):
    assert ctx.text_input("hello", None, "--text") == "hello"
    assert ctx.text_input(None, None, "--text") is None
    assert ctx.text_input("  ", None, "--text") == "  "


def test_text_input_reads_the_file_form(ctx, tmp_path):
    f = tmp_path / "body.txt"
    f.write_bytes(b"from file\n")
    assert ctx.text_input(None, str(f), "--text") == "from file\n"


def test_text_input_inline_and_file_are_exclusive(ctx, tmp_path):
    f = tmp_path / "body.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        ctx.text_input("inline", str(f), "--html")
    assert str(exc.value) == "--html and --html-file are mutually exclusive."


@pytest.mark.parametrize("content", ["", " \n\t\n", "﻿"])
def test_text_input_rejects_empty_file(ctx, tmp_path, content):
    f = tmp_path / "empty.txt"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        ctx.text_input(None, str(f), "--text")
    assert str(exc.value) == "--text-file is empty; nothing to send."


def test_text_input_rejects_empty_stdin(ctx, monkeypatch):
    feed(monkeypatch, b"")
    with pytest.raises(ValidationError) as exc:
        ctx.text_input(None, "-", "--message")
    assert str(exc.value) == "--message-file is empty; nothing to send."


def test_text_input_names_the_file_flag_for_the_stdin_rule(ctx, monkeypatch):
    feed(monkeypatch, b"body")
    assert ctx.text_input(None, "-", "--text") == "body"
    with pytest.raises(ValidationError, match="--html-file and --text-file both read stdin"):
        ctx.text_input(None, "-", "--html")


# -- the config-directory guard -----------------------------------------------
@reader
def test_guard_refuses_a_direct_path(ctx, cfg_dir, read):
    with pytest.raises(ValidationError) as exc:
        read(ctx, str(cfg_dir / "token.json"))
    assert str(exc.value) == (
        f"Refusing to read {cfg_dir / 'token.json'}: it is inside the hfox config directory."
    )
    assert exc.value.exit_code is ExitCode.VALIDATION


@reader
def test_guard_refuses_the_directory_itself(ctx, cfg_dir, read):
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(cfg_dir))


@reader
def test_guard_runs_before_the_existence_check(ctx, cfg_dir, read):
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(cfg_dir / "absent" / "x.txt"))


@reader
def test_guard_refuses_dot_dot_routes_into_the_directory(ctx, cfg_dir, tmp_path, read):
    (tmp_path / "other").mkdir()
    route = tmp_path / "other" / ".." / cfg_dir.name / "token.json"
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(route))


@needs_links
@reader
def test_guard_refuses_a_symlink_to_a_guarded_file(ctx, cfg_dir, tmp_path, read):
    link = tmp_path / "innocent.txt"
    link.symlink_to(cfg_dir / "token.json")
    with pytest.raises(ValidationError) as exc:
        read(ctx, str(link))
    assert str(exc.value) == f"Refusing to read {link}: it is inside the hfox config directory."


@needs_links
@reader
def test_guard_refuses_a_symlink_to_the_directory(ctx, cfg_dir, tmp_path, read):
    link = tmp_path / "shortcut"
    link.symlink_to(cfg_dir, target_is_directory=True)
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(link / "token.json"))
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(link))


@needs_links
@reader
def test_guard_resolves_a_symlinked_config_directory(tmp_path, monkeypatch, read):
    real = tmp_path / "real-store"
    real.mkdir()
    (real / "token.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "linked-cfg"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("HFOX_CONFIG_DIR", str(link))
    guarded = AppContext(config=load_config())
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(guarded, str(real / "token.json"))


@needs_links
@reader
def test_guard_refuses_a_hard_link_to_token_json(ctx, cfg_dir, tmp_path, read):
    link = tmp_path / "copy.json"
    try:
        os.link(cfg_dir / "token.json", link)
    except OSError:
        pytest.skip("hard links are unavailable here")
    # An empty secret list isolates the identity check from the content check.
    ctx._guard_secrets = ()
    with pytest.raises(ValidationError) as exc:
        read(ctx, str(link))
    assert str(exc.value) == f"Refusing to read {link}: it is inside the hfox config directory."


@reader
@pytest.mark.parametrize("dry_run", [False, True])
def test_config_dir_flag_cannot_move_the_guard(cfg_dir, tmp_path, read, dry_run):
    empty = tmp_path / "empty"
    empty.mkdir()
    moved = AppContext(config=load_config(str(empty)), dry_run=dry_run)
    assert moved.config.dir == empty
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(moved, str(cfg_dir / "token.json"))
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(moved, str(empty / "notes.txt"))


@reader
def test_guard_covers_the_default_directory(ctx, tmp_path, read):
    default = Path(os.environ["HOME"]) / ".config" / "hfox"
    default.mkdir(parents=True)
    (default / "token.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(default / "token.json"))


def test_guard_expands_the_home_directory(ctx, monkeypatch):
    default = Path(os.environ["HOME"]) / ".config" / "hfox"
    default.mkdir(parents=True)
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        ctx.read_text("~/.config/hfox/token.json", "--text-file")


def test_sibling_directory_sharing_the_name_prefix_is_allowed(ctx, cfg_dir, tmp_path):
    sibling = cfg_dir.with_name(cfg_dir.name + "-backup")
    sibling.mkdir()
    f = sibling / "note.txt"
    f.write_text("fine", encoding="utf-8")
    assert ctx.read_text(str(f), "--text-file") == "fine"
    assert ctx.attach({}, [str(f)])["files"] == [("attachments", ("note.txt", b"fine"))]
    assert check_readable_path(str(f), [cfg_dir]) == f


@pytest.mark.skipif(sys.platform != "darwin", reason="needs a case-insensitive filesystem")
@reader
def test_guard_refuses_an_upper_cased_path(ctx, cfg_dir, read):
    upper = cfg_dir.parent / cfg_dir.name.upper() / "TOKEN.JSON"
    if not upper.exists():
        pytest.skip("the filesystem is case-sensitive")
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        read(ctx, str(upper))


def test_check_readable_path_returns_the_expanded_unresolved_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert check_readable_path("rel/x.txt", [tmp_path / "cfg"]) == Path("rel/x.txt")
    assert check_readable_path("~/x.txt") == Path(os.environ["HOME"]) / "x.txt"


def _no_home(monkeypatch):
    """Make every `~` expansion fail the way an unknown user or a missing home does."""

    def fail(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", fail)


def test_unexpandable_tilde_path_is_used_as_written(ctx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "~draft.txt").write_bytes(b"body\n")
    (tmp_path / "~data.json").write_text("[1]", encoding="utf-8")
    _no_home(monkeypatch)
    assert check_readable_path("~draft.txt") == Path("~draft.txt")
    assert ctx.read_text("~draft.txt", "--text-file") == "body\n"
    assert ctx.read_json("~data.json", "--file") == [1]
    assert load_json_file("~data.json") == [1]
    assert ctx.attach({}, ["~draft.txt"])["files"] == [("attachments", ("~draft.txt", b"body\n"))]


@reader
def test_unexpandable_tilde_path_that_is_missing_is_not_found(ctx, tmp_path, monkeypatch, read):
    monkeypatch.chdir(tmp_path)
    _no_home(monkeypatch)
    with pytest.raises(ValidationError, match="not found: ~nouser") as exc:
        read(ctx, os.path.join("~nouser", "x.txt"))
    assert exc.value.exit_code is ExitCode.VALIDATION


def test_load_json_file_names_an_unexpandable_path_on_bad_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "~bad.json").write_text("{nope", encoding="utf-8")
    _no_home(monkeypatch)
    with pytest.raises(ValidationError, match="Invalid JSON in ~bad.json"):
        load_json_file("~bad.json")


def test_guard_holds_for_an_unexpandable_guarded_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "~cfg").mkdir()
    (tmp_path / "~cfg" / "token.json").write_text("{}", encoding="utf-8")
    _no_home(monkeypatch)
    with pytest.raises(ValidationError, match="inside the hfox config directory"):
        open_guarded(os.path.join("~cfg", "token.json"), forbidden=[Path("~cfg")])


def test_open_guarded_names_what_is_missing(tmp_path):
    with pytest.raises(ValidationError, match="Attachment not found: .*gone.bin"):
        open_guarded(str(tmp_path / "gone.bin"), what="Attachment")


def test_open_guarded_returns_an_open_binary_handle(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"\x00\x01")
    path, handle = open_guarded(str(f))
    with handle:
        assert path == f
        assert handle.read() == b"\x00\x01"


# -- the credential content check ---------------------------------------------
def test_check_no_secrets_message_never_holds_the_secret():
    with pytest.raises(ValidationError) as exc:
        check_no_secrets(b"key=" + ENV_KEY.encode() + b"\n", "stdin", [ENV_KEY])
    assert str(exc.value) == "Refusing to send stdin: it contains HappyFox credentials."
    assert ENV_KEY not in json.dumps(exc.value.to_dict())
    check_no_secrets(b"nothing here", "stdin", [ENV_KEY])
    check_no_secrets(b"anything", "stdin")


def _secret_ctx(monkeypatch, dry_run):
    monkeypatch.setenv("HFOX_API_KEY", ENV_KEY)
    return AppContext(config=load_config(), dry_run=dry_run)


def _refused(exc, *secrets):
    text = json.dumps(exc.value.to_dict())
    assert "it contains HappyFox credentials" in text
    assert exc.value.exit_code is ExitCode.VALIDATION
    for secret in (*secrets, ENV_KEY, FILE_KEY, FILE_CODE):
        assert secret not in text


@reader
@pytest.mark.parametrize("dry_run", [False, True])
def test_copy_of_token_json_outside_the_directory_is_refused(
    cfg_dir, tmp_path, monkeypatch, read, dry_run
):
    copy = tmp_path / "backup.json"
    copy.write_bytes((cfg_dir / "token.json").read_bytes())
    with pytest.raises(ValidationError) as exc:
        read(_secret_ctx(monkeypatch, dry_run), str(copy))
    _refused(exc)
    assert str(exc.value) == f"Refusing to send {copy}: it contains HappyFox credentials."


@reader
@pytest.mark.parametrize("dry_run", [False, True])
def test_file_holding_the_env_api_key_is_refused(tmp_path, cfg_dir, monkeypatch, read, dry_run):
    f = tmp_path / "notes.json"
    f.write_text(json.dumps({"note": f"key is {ENV_KEY}"}), encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        read(_secret_ctx(monkeypatch, dry_run), str(f))
    _refused(exc)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("secret", [ENV_KEY, FILE_KEY, FILE_CODE])
@pytest.mark.parametrize("kind", ["text", "json"])
def test_stdin_holding_credentials_is_refused(cfg_dir, monkeypatch, kind, secret, dry_run):
    feed(monkeypatch, json.dumps({"k": secret}).encode())
    with pytest.raises(ValidationError) as exc:
        READERS[kind](_secret_ctx(monkeypatch, dry_run), "-")
    _refused(exc)
    assert str(exc.value) == "Refusing to send stdin: it contains HappyFox credentials."


def test_short_credentials_are_not_scanned_for(cfg_dir, tmp_path, monkeypatch):
    (cfg_dir / "token.json").unlink()
    monkeypatch.setenv("HFOX_API_KEY", "k")
    monkeypatch.setenv("HFOX_AUTH_CODE", "c")
    f = tmp_path / "body.txt"
    f.write_text("kc and more", encoding="utf-8")
    assert AppContext(config=load_config()).read_text(str(f), "--text-file") == "kc and more"


@reader
@pytest.mark.parametrize("dry_run", [False, True])
def test_plain_file_outside_the_directory_passes(cfg_dir, tmp_path, monkeypatch, read, dry_run):
    f = tmp_path / "plain.json"
    f.write_text('{"ok": true}', encoding="utf-8")
    result = read(_secret_ctx(monkeypatch, dry_run), str(f))
    assert result in (
        '{"ok": true}',
        {"ok": True},
        {"data": {"a": "1"}, "files": [("attachments", ("plain.json", b'{"ok": true}'))]},
    )
