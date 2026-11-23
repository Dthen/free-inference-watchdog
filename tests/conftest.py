"""Shared pytest configuration for the standalone suite.

The single ``integration`` marker is assigned HERE, declaratively: a short,
explicit list of whole modules and test ids that genuinely build or install the
wheel, drive the publisher's real Git publication path, launch the CLI or the
generated page as a real process, or run the shared Python/Node corpus. Nothing
is inferred from source shape and no test asserts marker plumbing; every
ordinary case stays with its owner in the default (fast) selection that
``pyproject.toml`` configures.

The operator read-back check is OPTIONAL: it is neither a portable package/core
check nor a default-suite guarantee, so it runs only when the operator names the
wrapper explicitly. ``script_elements`` is the single HTMLParser extractor
shared by the build and README page checks.

No path default, no HOME lookup and no environment fallback exist for the
operator check: with no ``--operator-tick-wrapper=PATH`` the two read-back
checks report an actionable skip, and an explicitly supplied but unusable path
fails rather than skips.
"""

import os
from html.parser import HTMLParser
from pathlib import Path

import pytest

OPERATOR_OPTION = "--operator-tick-wrapper"

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Whole files that are integration from top to bottom: the publisher's real
#: Bash/Git publication path, and the shared batch corpus compared across the
#: Python core and the real Node CLI.
INTEGRATION_MODULES = (
    "tests/test_contract_parity.py",
    "tests/test_publish_site.py",
)

#: Individual integration cases: the ONE genuine wheel build, the ONE
#: clean-install installed-console run, the fresh-process CLI launches, and the
#: generated page's own scripts executed under the real Node CLI.
INTEGRATION_CASES = (
    "tests/test_distribution.py::test_wheel_payload_and_core_smoke",
    "tests/test_distribution.py::"
    "test_installed_console_clean_install_and_stdio_isolation",
    "tests/test_mcp_protocol.py::test_fresh_process_cli_help_launch_isolation",
    "tests/test_mcp_protocol.py::"
    "test_file_source_stdio_discovery_calls_and_invalid_requests",
    "tests/test_mcp_protocol.py::"
    "test_loopback_url_stdio_last_good_and_unavailable_source",
    "tests/test_readme.py::test_documented_stdio_arguments_launch_a_real_mcp_session",
    "tests/test_readme.py::"
    "test_documented_native_json_command_runs_on_the_pinned_fixture",
    "tests/test_catalog_cli.py::"
    "test_native_cli_process_is_sdk_free_and_emits_real_utf8_bytes",
    "tests/test_catalog_cli.py::"
    "test_native_shutdown_boundary_keeps_codes_and_never_flushes_a_dead_stream",
    "tests/test_catalog_cli.py::"
    "test_native_output_interrupts_are_clean_130_without_tracebacks",
    "tests/test_build_site.py::test_page_scripts_are_inert_without_the_native_api",
    "tests/test_build_site.py::test_page_bootstrap_registers_the_three_shared_tools_once",
    "tests/test_readme.py::test_documented_browser_setup_registers_the_page_tools",
    "tests/test_readme.py::"
    "test_documented_browser_payloads_match_the_embedded_snapshot",
    "tests/test_readme.py::"
    "test_browser_publication_status_is_bound_to_the_tracked_artifact",
)

OPERATOR_SKIP_REASON = (
    "operator read-back check: no deployed tick wrapper was named; pass "
    f"{OPERATOR_OPTION}=PATH to run the read-only checks against a specific "
    "wrapper (skipped by default so the portable suite never assumes a Hermes "
    "HOME or an installed wrapper)"
)


def _relative_module(item):
    """The item's module path relative to the repository root, POSIX-style."""
    path = getattr(item, "path", None)
    if path is None:
        path = getattr(item, "fspath", None)
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except (OSError, ValueError):
        return Path(path).as_posix()


def pytest_collection_modifyitems(config, items):
    """Tag the explicitly listed integration cases with the ONE marker.

    The keyword filter for the default ``-m "not integration"`` selection runs
    in a ``trylast`` hook, so a marker added here is always considered.
    """
    for item in items:
        if (_relative_module(item) in INTEGRATION_MODULES
                or item.nodeid in INTEGRATION_CASES):
            item.add_marker(pytest.mark.integration)


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


class _ScriptCollector(HTMLParser):
    """Collect every ``<script>`` as (attrs dict, EXACT inner text), in order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.open = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            assert self.open is None, "nested/unterminated <script> element"
            self.open = [dict(attrs), []]

    def handle_data(self, data):
        if self.open is not None:
            self.open[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.open is not None:
            self.scripts.append((self.open[0], "".join(self.open[1])))
            self.open = None


@pytest.fixture(scope="session")
def script_elements():
    """Callable: ordered (attrs dict, EXACT body) pairs for every ``<script>``.

    One owner for page-script extraction. Every call builds a fresh collector,
    so calls share no parse history; the session scope only lets the
    module-scoped page fixture request this callable.
    """

    def extract(html):
        collector = _ScriptCollector()
        collector.feed(html)
        collector.close()
        assert collector.open is None, "unterminated <script> element"
        return collector.scripts

    return extract
