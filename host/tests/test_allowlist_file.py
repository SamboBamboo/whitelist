"""Tier 3 — direct whitelist.json editing with the §7 ritual."""

import json
import os

import pytest

from whitelist_host.allowlist import AllowlistConflict, FileBackend

UUID_A = "00000000-0000-0000-0009-01f64f6dd58e"
UUID_B = "11111111-1111-1111-1111-111111111111"


def entries_on_disk(path):
    return json.loads(path.read_text())


def test_add_creates_entry_and_calls_reload(tmp_path):
    path = tmp_path / "whitelist.json"
    reloads = []
    backend = FileBackend(path, reload_cmd=lambda: reloads.append(1))
    backend.add(".Cave_Johnson", UUID_A, "bedrock")
    assert entries_on_disk(path) == [{"uuid": UUID_A, "name": ".Cave_Johnson"}]
    assert reloads == [1]
    assert not list(tmp_path.glob(".whitelist.json.tmp.*"))  # no temp litter


def test_add_is_idempotent_and_skips_reload(tmp_path):
    path = tmp_path / "whitelist.json"
    reloads = []
    backend = FileBackend(path, reload_cmd=lambda: reloads.append(1))
    backend.add("Foo_Bar", UUID_A, "java")
    backend.add("Foo_Bar", UUID_A, "java")
    assert len(entries_on_disk(path)) == 1
    assert reloads == [1]


def test_same_name_different_uuid_is_a_conflict(tmp_path):
    path = tmp_path / "whitelist.json"
    backend = FileBackend(path)
    backend.add("Foo_Bar", UUID_A, "java")
    with pytest.raises(AllowlistConflict):
        backend.add("Foo_Bar", UUID_B, "java")
    assert entries_on_disk(path) == [{"uuid": UUID_A, "name": "Foo_Bar"}]  # untouched


def test_mode_preserved_across_rewrite(tmp_path):
    path = tmp_path / "whitelist.json"
    path.write_text("[]")
    os.chmod(path, 0o640)
    backend = FileBackend(path)
    backend.add("Foo_Bar", UUID_A, "java")
    assert (os.stat(path).st_mode & 0o7777) == 0o640


def test_existing_entries_survive(tmp_path):
    path = tmp_path / "whitelist.json"
    path.write_text(json.dumps([{"uuid": UUID_B, "name": "OldTimer"}]))
    backend = FileBackend(path)
    backend.add("Foo_Bar", UUID_A, "java")
    disk = entries_on_disk(path)
    assert {"uuid": UUID_B, "name": "OldTimer"} in disk
    assert {"uuid": UUID_A, "name": "Foo_Bar"} in disk


def test_remove_and_idempotent_remove(tmp_path):
    path = tmp_path / "whitelist.json"
    backend = FileBackend(path)
    backend.add("Foo_Bar", UUID_A, "java")
    backend.remove("Foo_Bar", UUID_A, "java")
    assert entries_on_disk(path) == []
    backend.remove("Foo_Bar", UUID_A, "java")  # no error
    assert entries_on_disk(path) == []


def test_uuid_match_ignores_dashes_and_case(tmp_path):
    path = tmp_path / "whitelist.json"
    backend = FileBackend(path)
    backend.add("Foo_Bar", UUID_A, "java")
    backend.add("Foo_Bar", UUID_A.replace("-", "").upper(), "java")  # same identity
    assert len(entries_on_disk(path)) == 1
