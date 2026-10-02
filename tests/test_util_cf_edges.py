"""Edge cases of the _util.py and cf.py helpers: guards, attachments, bulk results, cf parsing."""

import ctypes
import errno
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

import hfox.cli._util as util_mod
from hfox.cli._util import (
    NOT_IN_GROUP,
    attach,
    check_no_secrets,
    check_readable_path,
    count_failures,
    filter_rows,
    fold,
    load_json_file,
    open_guarded,
    parse_json,
    read_text_file,
    validate_contact_ref,
)
from hfox.cli.cf import coerce_value, parse_cf_json, parse_cf_options
from hfox.core.errors import ExitCode, ValidationError

SECRET = "edge-api-key-0123456789"
REFUSED = "inside the hfox config directory"
LEAKS = "contains HappyFox credentials"

needs_links = pytest.mark.skipif(sys.platform == "win32", reason="links need privileges")

READERS = {
    "text": read_text_file,
    "json": load_json_file,
    "attach": lambda path: attach({}, [path]),
}
reader = pytest.mark.parametrize("read", READERS.values(), ids=READERS.keys())

# The ways a caller may hand over the guard list or the secret list.
spelled = pytest.mark.parametrize("spell", [list, iter], ids=["list", "one-shot iterable"])


def _store(root: Path) -> Path:
    """Create a guarded directory holding a token.json under `root`."""
    store = root / "store"
    store.mkdir(parents=True)
    (store / "token.json").write_text(json.dumps({"api_key": SECRET}), encoding="utf-8")
    return store


def _hard_link(target: Path, link: Path) -> Path:
    """Hard-link `link` to `target`, skipping the test where that is not possible."""
    try:
        os.link(target, link)
    except OSError:
        pytest.skip("hard links are unavailable here")
    return link


def _unresolvable(monkeypatch, name):
    """Make Path.resolve fail for any path whose last component is `name`."""
    real = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.name == name:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)


def _record_opens(monkeypatch):
    """Return a list that collects every handle Path.open returns from now on."""
    handles = []
    real = Path.open

    def recording(self, *args, **kwargs):
        handle = real(self, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", recording)
    return handles


def _all_closed(handles) -> bool:
    """True when at least one file was opened and none is still open."""
    return bool(handles) and all(handle.closed for handle in handles)


def _io_error(*args, **kwargs):
    raise OSError(errno.EIO, "Input/output error")


# -- validators ---------------------------------------------------------------
@pytest.mark.parametrize("value", ["a@x.org/tickets", "a@x org", "a@x.org ", "a@x.org\n"])
def test_validate_contact_ref_rejects_a_slash_or_whitespace_after_the_at_sign(value):
    with pytest.raises(ValidationError) as exc:
        validate_contact_ref(value)
    assert exc.value.exit_code is ExitCode.VALIDATION


# -- the Windows console probe ------------------------------------------------
@pytest.mark.parametrize("answer", [1, 0])
def test_has_console_is_whether_get_console_mode_succeeds_on_the_stream_handle(
    monkeypatch, tmp_path, answer
):
    asked = []

    def get_console_mode(handle, mode):
        asked.append(handle.value)
        return answer

    # Stand-ins for msvcrt and kernel32, so the console answer is under the test's control.
    msvcrt = types.SimpleNamespace(get_osfhandle=lambda fd: fd + 1000)
    windll = types.SimpleNamespace(kernel32=types.SimpleNamespace(GetConsoleMode=get_console_mode))
    with open(tmp_path / "f", "w+") as stream, monkeypatch.context() as patch:
        patch.setitem(sys.modules, "msvcrt", msvcrt)
        patch.setattr(ctypes, "windll", windll, raising=False)
        assert util_mod._has_console(stream) is bool(answer)
        assert asked == [stream.fileno() + 1000]


# -- the config-directory guard -----------------------------------------------
@reader
def test_path_that_cannot_be_resolved_is_refused(tmp_path, monkeypatch, read):
    f = tmp_path / "loop.json"
    f.write_text("[1]", encoding="utf-8")
    _unresolvable(monkeypatch, "loop.json")
    with pytest.raises(ValidationError) as exc:
        read(str(f))
    assert str(f) in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


def test_unresolvable_guard_directory_leaves_the_other_guards_on(tmp_path, monkeypatch):
    store = _store(tmp_path)
    outside = tmp_path / "note.txt"
    outside.write_text("fine", encoding="utf-8")
    _unresolvable(monkeypatch, "broken")
    forbidden = [tmp_path / "broken", store]
    with pytest.raises(ValidationError, match=REFUSED):
        check_readable_path(str(store / "token.json"), forbidden)
    assert check_readable_path(str(outside), forbidden) == outside


def test_guard_directory_written_with_a_tilde_is_expanded():
    store = _store(Path(os.environ["HOME"]))
    with pytest.raises(ValidationError, match=REFUSED):
        check_readable_path(str(store / "token.json"), [Path("~/store")])


@needs_links
@pytest.mark.parametrize(
    "spell",
    [lambda store: iter([store]), lambda store: [Path("~") / store.name]],
    ids=["one-shot iterable", "tilde path"],
)
def test_hard_link_to_token_json_is_refused_however_the_guard_is_spelled(tmp_path, spell):
    store = _store(Path(os.environ["HOME"]))
    link = _hard_link(store / "token.json", tmp_path / "copy.json")
    with pytest.raises(ValidationError, match=REFUSED):
        open_guarded(str(link), forbidden=spell(store))


def test_load_json_file_applies_the_guard_and_the_credential_check(tmp_path):
    store = _store(tmp_path)
    copy = tmp_path / "copy.json"
    copy.write_bytes((store / "token.json").read_bytes())
    with pytest.raises(ValidationError, match=REFUSED):
        load_json_file(str(store / "token.json"), forbidden=[store])
    with pytest.raises(ValidationError, match=LEAKS):
        load_json_file(str(copy), secrets=[SECRET])


# -- open handles -------------------------------------------------------------
@needs_links
def test_refused_hard_link_is_not_left_open(tmp_path, monkeypatch):
    store = _store(tmp_path)
    link = _hard_link(store / "token.json", tmp_path / "copy.json")
    handles = _record_opens(monkeypatch)
    with pytest.raises(ValidationError, match=REFUSED):
        open_guarded(str(link), forbidden=[store])
    assert _all_closed(handles)


def test_file_whose_status_cannot_be_read_is_refused_and_closed(tmp_path, monkeypatch):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    handles = _record_opens(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", _io_error)
        with pytest.raises(ValidationError) as exc:
            open_guarded(str(f))
    assert str(f) in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION
    assert _all_closed(handles)


def test_read_text_file_closes_the_file_after_a_read_and_after_a_read_error(tmp_path, monkeypatch):
    f = tmp_path / "body.txt"
    f.write_bytes(b"body\n")
    handles = _record_opens(monkeypatch)
    assert read_text_file(str(f)) == "body\n"
    assert _all_closed(handles)
    handles.clear()
    monkeypatch.setattr(util_mod, "_read_all", _io_error)
    with pytest.raises(ValidationError):
        read_text_file(str(f))
    assert _all_closed(handles)


def test_attach_closes_every_file_also_when_a_later_one_is_refused(tmp_path, monkeypatch):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"first")
    b.write_bytes(b"second")
    handles = _record_opens(monkeypatch)
    assert len(attach({}, [str(a), str(b)])["files"]) == 2
    assert _all_closed(handles)
    handles.clear()
    with pytest.raises(ValidationError):
        attach({}, [str(a), str(tmp_path / "gone.bin")])
    assert _all_closed(handles)


# -- read errors --------------------------------------------------------------
@pytest.mark.parametrize("read", [read_text_file, load_json_file], ids=["text", "json"])
def test_read_error_is_a_validation_error_naming_the_file(tmp_path, monkeypatch, read):
    f = tmp_path / "flaky.json"
    f.write_text("[1]", encoding="utf-8")
    monkeypatch.setattr(util_mod, "_read_all", _io_error)
    with pytest.raises(ValidationError) as exc:
        read(str(f))
    assert str(f) in str(exc.value)
    assert exc.value.exit_code is ExitCode.VALIDATION


# -- the credential content check ---------------------------------------------
def test_check_no_secrets_matches_the_utf8_bytes_of_a_non_ascii_secret():
    secret = "clé-secrète-0123456789"
    with pytest.raises(ValidationError, match=LEAKS):
        check_no_secrets(f"note: {secret}\n".encode(), "stdin", [secret])


def test_check_no_secrets_ignores_an_empty_secret():
    check_no_secrets(b"any content", "stdin", ["", SECRET])


# -- attachments --------------------------------------------------------------
def test_attach_with_an_empty_file_list_sends_the_json_body():
    body = {"subject": "s", "assignee": None}
    assert attach(body, []) == {"json": body}


def test_attach_sends_files_in_the_order_given(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"first")
    b.write_bytes(b"second")
    assert attach({}, [str(b), str(a)])["files"] == [
        ("attachments", ("b.bin", b"second")),
        ("attachments", ("a.bin", b"first")),
    ]


def test_attach_multipart_sends_every_value_but_none_as_text(tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello")
    body = {
        "zero": 0,
        "ratio": 1.5,
        "blank": "",
        "off": False,
        "none": None,
        "no_ids": [],
        "custom_fields": {"5": 4, "6": [1, 2]},
    }
    assert attach(body, [str(f)])["data"] == {
        "zero": "0",
        "ratio": "1.5",
        "blank": "",
        "off": "false",
        "no_ids": "[]",
        "custom_fields": '{"5": 4, "6": [1, 2]}',
    }


@spelled
def test_attach_guards_every_file_not_only_the_first(tmp_path, spell):
    store = _store(tmp_path)
    clean = tmp_path / "clean.txt"
    clean.write_bytes(b"nothing here")
    with pytest.raises(ValidationError, match=REFUSED):
        attach({}, [str(clean), str(store / "token.json")], forbidden=spell([store]))


@spelled
def test_attach_checks_every_file_for_credentials(tmp_path, spell):
    clean, leaky = tmp_path / "clean.txt", tmp_path / "leaky.txt"
    clean.write_bytes(b"nothing here")
    leaky.write_bytes(b"key=" + SECRET.encode())
    with pytest.raises(ValidationError, match=LEAKS):
        attach({}, [str(clean), str(leaky)], secrets=spell([SECRET]))


def test_attach_limit_applies_to_the_bytes_read_from_all_files_together(tmp_path, monkeypatch):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    # Each file alone stays under the limit after growing; the two together do not.
    monkeypatch.setattr(util_mod, "MAX_ATTACHMENT_BYTES", 4)
    monkeypatch.setattr(util_mod, "_read_all", lambda handle: b"123")
    with pytest.raises(ValidationError, match="limit"):
        attach({}, [str(a), str(b)])


# -- strict JSON --------------------------------------------------------------
def test_parse_json_keeps_finite_floats_as_numbers():
    assert parse_json("[1.5, -2.25e2, 1e-3]", "--file") == [1.5, -225.0, 0.001]
    assert parse_cf_json('{"7": 0.5}') == {"t-cf-7": 0.5}


def test_parse_json_rejects_a_negative_number_beyond_float_range():
    with pytest.raises(ValidationError, match="--file"):
        parse_json("[-1e400]", "--file")


# -- bulk results -------------------------------------------------------------
def test_count_failures_counts_only_entries_reporting_success_false():
    result = [{"id": 1}, {"success": None}, {"success": True}, "text", None, 7, {"success": False}]
    assert count_failures(result) == 1


@pytest.mark.parametrize(
    "entry",
    [
        {"errors": [{"field": "contact", "errors": ["Select a valid choice."]}], "success": False},
        {"data": NOT_IN_GROUP, "success": False},
        {"data": {"message": f"{NOT_IN_GROUP} or the account", "contact": 3}, "success": False},
    ],
    ids=["no data", "data is the message itself", "message only contains it"],
)
def test_count_failures_only_the_exact_benign_message_is_not_a_failure(entry):
    assert count_failures([entry], benign=NOT_IN_GROUP) == 1


# -- local filters ------------------------------------------------------------
def test_fold_casefolds_what_compatibility_normalization_produces():
    assert fold("㎒") == "mhz"
    row = {"name": "100 ㎒ link"}
    assert filter_rows([row], {"name": "MHZ"}) == [row]


# -- custom fields ------------------------------------------------------------
@pytest.mark.parametrize("raw", ["[", "]", "[a,b", "a,b]", " [4]", "[4] "])
def test_coerce_value_needs_both_brackets_for_a_list(raw):
    assert coerce_value(raw) == raw


@pytest.mark.parametrize("raw", ["[ ]", "[ \t ]"])
def test_coerce_value_whitespace_only_brackets_are_an_empty_list(raw):
    assert coerce_value(raw) == []


def test_parse_cf_options_keeps_the_value_text_exactly():
    assert parse_cf_options(["7= 42", "8= padded "]) == {"t-cf-7": " 42", "t-cf-8": " padded "}


@pytest.mark.parametrize(
    ("items", "named"),
    [
        (["7=ok", "nope"], ["nope"]),
        (["7=[1,,2]"], ["[1,,2]"]),
        (["7=  "], ["7=  ", "--cf-json"]),
    ],
    ids=["no equals sign", "blank list item", "blank value"],
)
def test_parse_cf_options_errors_name_the_offending_item(items, named):
    with pytest.raises(ValidationError) as exc:
        parse_cf_options(items)
    assert [text for text in named if text not in str(exc.value)] == []


@pytest.mark.parametrize(
    ("kwargs", "forms"),
    [
        ({}, ["<id>", "t-cf-<id>"]),
        ({"prefix": "c-cf-"}, ["<id>", "c-cf-<id>"]),
        ({"prefix": "t-cf-", "allowed": ("t-cf-", "ccf-")}, ["<id>", "t-cf-<id>", "ccf-<id>"]),
        ({"prefix": "", "allowed": ()}, ["<id>"]),
        ({"prefix": ""}, ["<id>"]),
    ],
    ids=["default", "contact prefix", "two allowed", "no allowed prefix", "blank prefix"],
)
def test_key_error_lists_the_forms_the_call_site_accepts(kwargs, forms):
    with pytest.raises(ValidationError) as exc:
        parse_cf_options(["x-9=1"], **kwargs)
    assert sorted(re.findall(r"[\w-]*<id>", str(exc.value))) == sorted(forms)
