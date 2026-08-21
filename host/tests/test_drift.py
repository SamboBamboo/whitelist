"""§4 config-drift guard: the daemon must refuse to match when the Worker's
pinned normalization config disagrees with the real Floodgate config or with
this code's contract version."""

import pytest

from whitelist_host.floodgate import (
    DriftError,
    FloodgateSettings,
    check_drift,
    read_floodgate_config,
)

GOOD = {"username_prefix": ".", "replace_spaces": True, "version": 1}
FG = FloodgateSettings(prefix=".", replace_spaces=True)


def test_matching_config_passes():
    assert check_drift(GOOD, FG) == []


@pytest.mark.parametrize(
    "worker_norm",
    [
        dict(GOOD, username_prefix="!"),
        dict(GOOD, replace_spaces=False),
        dict(GOOD, version=2),
        {},
    ],
)
def test_any_mismatch_is_reported(worker_norm):
    assert check_drift(worker_norm, FG) != []


def test_read_floodgate_config(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "# floodgate config\n"
        "username-prefix: \".\"\n"
        "replace-spaces: true\n"
        "other-stuff: 1\n"
    )
    fg = read_floodgate_config(cfg)
    assert fg.prefix == "."
    assert fg.replace_spaces is True


def test_missing_floodgate_config_refuses(tmp_path):
    with pytest.raises(DriftError):
        read_floodgate_config(tmp_path / "nope.yml")


def test_floodgate_config_without_keys_refuses(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("something-else: true\n")
    with pytest.raises(DriftError):
        read_floodgate_config(cfg)
