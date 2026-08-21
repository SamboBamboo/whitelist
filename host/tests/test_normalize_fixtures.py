"""Normalization fixtures (§4) — the Python side of the shared contract.
The same JSON drives the TypeScript suite; both must stay green together."""

import json
from pathlib import Path

import pytest

from whitelist_host.normalize import normalize_form, normalize_logged

FIXTURES = json.loads(
    (Path(__file__).parent.parent.parent / "shared" / "normalization-fixtures.json").read_text()
)


def run_case(case, config):
    fn = normalize_form if case["source"] == "form" else normalize_logged
    return fn(
        case["platform"],
        case["input"],
        prefix=config["username_prefix"],
        replace_spaces=config["replace_spaces"],
    )


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["id"])
def test_fixture_case(case):
    got = run_case(case, case["config"])
    expect = case["expect"]
    if expect["ok"]:
        assert got.ok, f"{case['id']}: expected ok, got error {got.error}"
        assert got.normalized == expect["normalized"], case["id"]
    else:
        assert not got.ok, f"{case['id']}: expected error, got {got.normalized!r}"
        assert got.error == expect["error"], case["id"]


DEFAULT_CFG = {"username_prefix": ".", "replace_spaces": True}


def test_distinct_pairs_stay_distinct():
    for pair in FIXTURES["distinct_pairs"]:
        a = run_case(pair["a"], DEFAULT_CFG)
        b = run_case(pair["b"], DEFAULT_CFG)
        assert a.ok and b.ok
        assert a.normalized != b.normalized


def test_colliding_pairs_collide():
    for pair in FIXTURES["colliding_pairs"]:
        a = run_case(pair["a"], DEFAULT_CFG)
        b = run_case(pair["b"], DEFAULT_CFG)
        assert a.ok and b.ok
        assert a.normalized == b.normalized


def test_contract_version_matches_code():
    from whitelist_host import NORMALIZATION_VERSION

    assert FIXTURES["normalization_version"] == NORMALIZATION_VERSION
