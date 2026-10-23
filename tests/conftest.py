"""Shared pytest configuration for the standalone suite.

Deliberately minimal. The only suite-wide concern is the OPTIONAL operator
read-back of a DEPLOYED tick wrapper (Task 9 separation): it is neither a
portable package/core check nor a default-suite guarantee, so it runs only when
the operator names the wrapper explicitly.

No path default, no HOME lookup and no environment fallback exist here: with no
``--operator-tick-wrapper=PATH`` the two read-back checks report an actionable
skip, and an explicitly supplied but unusable path fails rather than skips.
"""

import os
from pathlib import Path

import pytest

OPERATOR_OPTION = "--operator-tick-wrapper"

OPERATOR_SKIP_REASON = (
    "operator read-back check: no deployed tick wrapper was named; pass "
    f"{OPERATOR_OPTION}=PATH to run the read-only checks against a specific "
    "wrapper (skipped by default so the portable suite never assumes a Hermes "
    "HOME or an installed wrapper)"
)


def pytest_addoption(parser):
    parser.addoption(
        OPERATOR_OPTION,
        action="store",
        dest="operator_tick_wrapper",
        default=None,
        metavar="PATH",
        help=(
            "explicit path to the deployed tick wrapper; opts the two read-only "
            "operator read-back checks into this run (default: skip them)"
        ),
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "operator: read-only read-back of an EXPLICITLY supplied deployed tick "
        f"wrapper; skipped unless {OPERATOR_OPTION}=PATH is given",
    )


@pytest.fixture
def operator_tick_wrapper(request):
    """The explicitly supplied tick wrapper path, validated as a readable file.

    Nothing is constructed, probed or executed when the option is absent: the
    check skips before any path is derived.
    """
    supplied = request.config.getoption("operator_tick_wrapper")
    if supplied is None:
        pytest.skip(OPERATOR_SKIP_REASON)

    path = Path(supplied)
    if not path.exists():
        pytest.fail(
            f"operator tick wrapper not found: {path} "
            f"({OPERATOR_OPTION}={supplied}); no HOME or installed-path "
            "fallback is attempted"
        )
    if not path.is_file():
        pytest.fail(
            f"operator tick wrapper is not a regular file: {path} "
            f"({OPERATOR_OPTION}={supplied})"
        )
    if not os.access(path, os.R_OK):
        pytest.fail(f"operator tick wrapper is not readable: {path}")
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        pytest.fail(f"operator tick wrapper is not readable: {path} ({exc})")
    return path
