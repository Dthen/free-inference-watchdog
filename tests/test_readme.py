"""Tests for README contracts that must not silently regress (F-R2-4).

Two audiences, both pinned here.

Consumer route (unit 10a): the README opens with purpose, the dashboard and the
standalone MCP quick start, and only THEN hands off to operator setup. The
documented install uses the immutable commit the examples were verified against
(never a branch), the generic stdio client block is a real `command`/`args`
vector that is actually launched over MCP stdio below, and the worked example's
stable values are re-derived from the pinned synthetic fixture rather than
trusted. The MCP route is pinned as catalogue QUERYING — no provider
credentials, no cron, no local roster — and it must keep saying that the
production JSON feed is not published yet and that WebMCP is planned, never a
remotely connectable MCP HTTP URL. That hand-off must not cost existing readers
anything: the baseline `#quick-start` anchor stays resolvable, and the `source`
block is pinned as provenance — a last-known-good replay is `ok: true` too, so it
never doubles as proof that the refresh succeeded.

Consumer metadata, freshness and identity guidance (unit 10b1) documents
behaviour that already exists, so its examples are checked against that
behaviour instead of against prose: the worked filter table and the paged
response are re-derived from the immutable fixture, rows that differ only in
filters must still answer differently (a wrong documented argument or result
would make them collide), `min_context_length` is exercised at its inclusive
boundary and against unknown metadata, the three freshness shapes are bound to
the fixture clock, and a real last-known-good replay must NOT be documented as
an identical `source` block (the 10a cache finding). Count scopes and the
operator fields a published snapshot cannot observe are pinned the same way.

Operator route (F-R2-4): delivery topology (operator decision, 2026-08-26) is
unchanged — the webhook in ~/.hermes/.env is the ONLY alert path and the cron job
is silent (`--deliver local`). The wrapper's job is diagnosability, not paging:
exit 1 (routine partial outage) stays fully silent; anything else prints to
STDERR and exits non-zero for the operator to find. The registration section is
pinned to the real Hermes cron CLI surface: `hermes cron register` does not exist
and `discord-home` is not a valid --deliver token — both invalid forms regressed
here once already, so their absence is pinned below.
"""

import asyncio
import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
FIXTURE = REPO / "tests" / "fixtures" / "catalog" / "lookup.json"
TOOL_CONTRACT = REPO / "watchdog_catalog" / "tools.json"

CONSUMER_HEADING = "## Query the catalogue over MCP"
OPERATOR_HEADING = "## Self-hosting the watchdog"
DASHBOARD_URL = "https://models.dthen.xyz"

#: The immutable commit this README's install and example commands were verified
#: against. A branch name is a moving target, so a floating ref is a regression.
PINNED_REF = "bd03dcbd8edcf72ceab0a322891fffc6ecb53ecc"
PINNED_FIXTURE_URL = (
    "https://raw.githubusercontent.com/Dthen/free-inference-watchdog/"
    f"{PINNED_REF}/tests/fixtures/catalog/lookup.json")
INSTALL_REF_PATTERN = re.compile(
    r"git\+https://github\.com/Dthen/free-inference-watchdog\.git"
    r"@([0-9A-Za-z._-]+)")

CONSOLE = "free-inference-watchdog-mcp"
SESSION_TIMEOUT_S = 120

#: The one source flag the documented client vector may select, mapped to the
#: `select_source()` mode the shipped CLI returns for it.
SOURCE_FLAGS = {"--catalog-url": "url", "--catalog-file": "file"}


def _readme():
    return README.read_text(encoding="utf-8")


def _fenced_blocks(text):
    """Return the contents of every ``` fenced code block in the README."""
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.S)


def _fenced_languages(text):
    """Return ``(language, body)`` for every fenced block, in document order."""
    return re.findall(r"```([a-z]*)\n(.*?)```", text, re.S)


def _section(text, heading):
    """The README body from ``heading`` up to the next ``## `` heading.

    Scoping assertions to one section means operator prose (or the frozen
    contract) can never accidentally satisfy a consumer-route assertion.
    """
    start = text.index(heading)
    body = text[start + len(heading):]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


def _paragraphs(text):
    """Whitespace-normalized paragraphs, so prose assertions ignore wrapping."""
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text)]


def _plain(text):
    """Prose with markdown emphasis and code ticks removed.

    ``**not** need`` and ``not need`` must read the same to a prose assertion;
    the README is allowed to emphasize, but the claim itself is what is pinned.
    """
    return text.replace("**", "").replace("`", "")


def _paragraph_with(text, needle):
    """The single paragraph containing ``needle`` (case-insensitive)."""
    wanted = needle.lower()
    matches = [p for p in _paragraphs(text) if wanted in p.lower()]
    assert matches, f"README has no paragraph mentioning {needle!r}"
    return matches[0]


def _heading_slugs(text):
    """GitHub-style anchors for every heading OUTSIDE fenced code blocks."""
    slugs = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        title = line.lstrip("#").strip().lower()
        title = "".join(ch for ch in title
                        if ch.isalnum() or ch in " -_")
        slugs.add(title.replace(" ", "-"))
    return slugs


def _client_config():
    """The documented generic stdio client block, parsed."""
    for language, body in _fenced_languages(_readme()):
        if language != "json":
            continue
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict) and "mcpServers" in document:
            return document
    raise AssertionError("README has no fenced JSON stdio client block")


def _documented_server():
    """The one documented stdio server entry: ``command`` plus ``args``."""
    servers = _client_config()["mcpServers"]
    assert list(servers) == ["free-inference-watchdog"], servers
    entry = servers["free-inference-watchdog"]
    assert set(entry) == {"command", "args"}, entry
    return entry


def _documented_args():
    args = list(_documented_server()["args"])
    assert args and all(isinstance(arg, str) and arg for arg in args), args
    return args


def _tool_names():
    contract = json.loads(TOOL_CONTRACT.read_text(encoding="utf-8"))
    return [entry["name"] for entry in contract]


# -- consumer metadata, freshness and identity examples (unit 10b1) ----------
#
# That guidance documents EXISTING behaviour, so its examples are bound to the
# real pure core over the immutable fixtures rather than restated as prose: the
# filter table and the paged response are re-derived, and each documented row
# must still discriminate between the filters it claims to distinguish.

METADATA_HEADING = "### Metadata search semantics"
CANNOT_HEADING = "### What the catalogue cannot tell you"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"


def _snapshot(name="lookup.json"):
    """One validated immutable fixture snapshot."""
    from watchdog_catalog import catalog

    return catalog.load_snapshot((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _matches(snapshot, arguments):
    """``provider / raw id`` labels for every endpoint a search returns."""
    from watchdog_catalog import query

    result = query.search_models(snapshot, arguments)
    assert result["ok"] is True, result
    return [f"{row['provider']} / {row['model_id']}" for row in result["endpoints"]]


def _filter_table():
    """The worked filter table as ``[(arguments, documented match labels)]``.

    A row's result cell is ``none`` or a list of ``provider / raw id`` labels;
    anything that is neither would fail the equality check below, so only the
    labels are extracted here.
    """
    text = _readme()
    body = text[text.index(METADATA_HEADING):]
    rows = []
    in_table = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("| Arguments "):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            break
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 2 or set(cells[0]) <= set("-: "):
            continue
        labels = [label for label in re.findall(r"`([^`]+)`", cells[1])
                  if " / " in label]
        rows.append((json.loads(cells[0].strip("`")), labels))
    return rows


def _bullets(heading):
    """The prose blocks of ONE ``###`` subsection, fenced examples excluded.

    Splitting on a single blank line keeps a list written without blank lines
    between items together, while dropping fenced blocks means an example can
    never satisfy a claim that belongs to the guidance bullets.
    """
    text = _readme()
    body = text[text.index(heading) + len(heading):]
    for stop in ("\n### ", "\n## "):
        end = body.find(stop)
        if end != -1:
            body = body[:end]
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    return [block for block in _paragraphs(_plain(body)) if block]


def _paged_example():
    """The documented paged search response, parsed."""
    for language, body in _fenced_languages(_readme()):
        if language == "json" and '"next_offset"' in body:
            return json.loads(body)
    raise AssertionError("README has no paged search example")


def test_readme_opens_with_purpose_and_dashboard_before_operator_setup():
    """A newcomer must see what this is, where the catalogue is, and which of the
    two routes they want, before any operator setup appears."""
    text = _readme()
    intro = text[:text.index(CONSUMER_HEADING)]

    assert DASHBOARD_URL in intro, "dashboard link is missing from the opening"
    assert "catalogue" in intro.lower(), "opening does not say what is published"
    assert text.count(OPERATOR_HEADING) == 1, "operator hand-off heading drifted"
    assert text.index(CONSUMER_HEADING) < text.index(OPERATOR_HEADING), \
        "consumer quick start must come BEFORE the operator hand-off"
    assert text.index(OPERATOR_HEADING) < text.index("## Cron registration"), \
        "operator setup must stay below the hand-off"


def test_consumer_section_scopes_queries_away_from_inference():
    """The MCP route must read as catalogue QUERYING: no keys, no cron, no local
    roster, no Hermes agent, no inference access and no hosted HTTP endpoint —
    and it must not leak operator setup steps into the consumer path."""
    section = _section(_readme(), CONSUMER_HEADING)
    lowered = _plain(section).lower()

    for phrase in ("provider api keys", "discord webhook", "cron job",
                   "providers/", "state/"):
        assert phrase in lowered, f"consumer scope omits {phrase!r}"
    assert "does not need" in lowered, \
        "consumer section never says what is NOT required"
    # The package is a plain Python install: the no-need list must name Hermes
    # itself, in the same sentence, not merely mention the word elsewhere
    # (QUALITY 10a finding 2 — the list stopped short of the agent).
    no_need = _paragraph_with(_plain(section), "does not need").lower()
    assert "hermes agent" in no_need, \
        "consumer no-need list must say the Hermes agent is not needed"
    assert "provider api keys" in no_need and "cron job" in no_need, \
        "the Hermes statement drifted out of the consumer no-need list"
    assert "is not inference" in lowered, \
        "consumer section must separate querying from inference"
    assert "remotely connectable" in lowered, \
        "consumer section must deny a hosted MCP HTTP endpoint"
    # The two operator traps a newcomer could follow by accident.
    assert ".env" not in section, "consumer path must not mention copying .env"
    assert "--init" not in section, "consumer path must not mention --init"


def test_install_examples_use_the_pinned_immutable_commit():
    """Install examples must pin a real commit — the one the examples were
    exercised against — never a branch, a placeholder or an unpublished sha."""
    text = _readme()
    refs = INSTALL_REF_PATTERN.findall(text)
    assert refs, "no git install reference documented"
    assert set(refs) == {PINNED_REF}, f"install refs drifted: {sorted(set(refs))}"
    assert re.fullmatch(r"[0-9a-f]{40}", PINNED_REF), "pinned ref is not a full sha"
    assert f"free-inference-watchdog-mcp @ git+https://github.com/" \
           f"Dthen/free-inference-watchdog.git@{PINNED_REF}" in text, \
        "the pip direct-reference spelling lost the pinned ref"

    lowered = _plain(_section(text, CONSUMER_HEADING)).lower()
    for floating in ("@master", "@main", "@latest", "<sha>", "commit_sha"):
        assert floating not in lowered, f"floating install ref {floating!r} is back"


def test_documented_stdio_client_block_is_a_real_launchable_vector():
    """The JSON block must carry the real console name and args, and the shipped
    CLI must actually accept that vector (parsed, not merely spelled)."""
    from watchdog_catalog.__main__ import PROGRAM, build_parser, select_source

    entry = _documented_server()
    assert PROGRAM == CONSOLE
    assert entry["command"] == CONSOLE, entry["command"]

    args = _documented_args()
    flags = [arg for arg in args if arg in SOURCE_FLAGS]
    assert len(flags) == 1, f"exactly one source flag expected, got {flags}"
    flag = flags[0]
    value = args[args.index(flag) + 1]
    assert not value.startswith("--"), f"flag {flag} has no value"

    arguments = build_parser().parse_args(args)
    kind, location = select_source(arguments)
    assert (kind, location) == (SOURCE_FLAGS[flag], value), (kind, location)

    # The block's working target is the pinned SYNTHETIC fixture, labelled as
    # such — never an unavailable production feed presented as the live route.
    assert flag == "--catalog-url", flag
    assert value == PINNED_FIXTURE_URL, value
    section = _section(_readme(), CONSUMER_HEADING)
    assert "synthetic" in section.lower(), "the fixture preview is not labelled"


def test_documented_cli_flags_exist_in_the_shipped_help():
    """Behavioural: the shipped CLI advertises exactly the documented flags."""
    completed = subprocess.run(
        [sys.executable, "-m", "watchdog_catalog", "--help"],
        cwd=str(REPO), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=SESSION_TIMEOUT_S)
    assert completed.returncode == 0, completed.stderr
    for flag in list(SOURCE_FLAGS) + ["--cache-dir"]:
        assert flag in completed.stdout, f"--help omits {flag}"


def test_documented_stdio_arguments_launch_a_real_mcp_session(tmp_path):
    """The documented argument vector is EXERCISED, not parsed: the same flags
    are launched over real MCP stdio and a metadata search is answered.

    The one substitution is the documented ``--catalog-url`` + pinned fixture URL
    pair, replaced by ``--catalog-file`` and the repository copy of that same
    fixture, so the check stays offline. Everything else — console name, flag
    spelling, argument order, stdio transport — is the documented vector.
    """
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = _documented_args()
    index = args.index("--catalog-url")
    assert args[index + 1] == PINNED_FIXTURE_URL
    offline_args = list(args)
    offline_args[index] = "--catalog-file"
    offline_args[index + 1] = str(FIXTURE)
    offline_args += ["--cache-dir", str(tmp_path / "cache")]

    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "watchdog_catalog", *offline_args],
        env=environment, cwd=str(REPO))

    async def exercise():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool(
                    "search_models", {"query": "shared", "limit": 5})
                text = result.content[0].text
                return initialized, listed, json.loads(text)

    initialized, listed, payload = asyncio.run(
        asyncio.wait_for(exercise(), SESSION_TIMEOUT_S))

    assert initialized.server_info.name == CONSOLE
    assert [tool.name for tool in listed.tools] == _tool_names()
    assert payload["ok"] is True, payload
    assert payload["source"]["mode"] == "file", payload["source"]
    assert payload["query"] == "shared"
    assert payload["returned_count"] == 2
    assert payload["total_matches"] == 2
    assert payload["counts"] == {"gateways": 2, "endpoints": 2,
                                 "distinct_raw_ids": 1,
                                 "marker_group_labels": 1}
    assert [row["provider"] for row in payload["endpoints"]] \
        == ["synthetic-a", "synthetic-b"]
    assert {row["model_id"] for row in payload["endpoints"]} == {"vendor/shared"}


def test_worked_example_values_re_derive_from_the_pinned_fixture():
    """The worked example must be real output: its stable fields are re-derived
    from the synthetic fixture through the pure core, and its staleness claim is
    checked against the fixture's own frozen tick."""
    from watchdog_catalog import catalog, query

    example = None
    for language, body in _fenced_languages(_readme()):
        if language == "json" and '"returned_count"' in body:
            example = json.loads(body)
            break
    assert example is not None, "no worked-example JSON block found"

    snapshot = catalog.load_snapshot(FIXTURE.read_text(encoding="utf-8"))
    real = query.search_models(snapshot, {"query": example["query"],
                                          "limit": example["limit"]
                                          if "limit" in example else 5})
    for key in ("ok", "query", "returned_count", "total_matches", "counts",
                "provider_staleness"):
        assert example[key] == real[key], f"example {key} is not real output"

    status = query.watchdog_status(snapshot, {}, now=int(time.time()))
    assert status["tick_fresh"] is False
    assert "tick_stale" in status["warnings"]
    assert example["source"]["warnings"] == ["tick_stale"]
    assert example["source"]["observation_fresh"] is False
    assert example["source"]["mode"] == "url"


def test_consumer_section_labels_the_feed_and_webmcp_as_unavailable():
    """The JSON feed is documented but explicitly NOT published, and WebMCP is
    documented as planned — never as shipped, and never as a remote HTTP URL."""
    section = _section(_readme(), CONSUMER_HEADING)
    lowered = _plain(section).lower()

    webmcp = [p for p in _paragraphs(_plain(section))
              if "webmcp" in p.lower() and "planned" in p.lower()]
    assert webmcp, "the WebMCP planned-status paragraph is gone"
    assert any("not available" in p.lower() for p in webmcp), \
        "WebMCP must be labelled not available"
    assert "not a remotely connectable mcp http url" in lowered, \
        "WebMCP must be distinguished from a remote MCP HTTP URL"

    assert "## Public JSON feed" in _readme(), "public JSON feed section is gone"
    assert "not yet published" in lowered, "feed status heading lost its gate"
    for phrase in ("schema_version", "snapshot_id", "tick_epoch",
                   "provider_order", "providers"):
        assert f"`{phrase}`" in section, f"feed schema omits {phrase}"

    assert "not live yet" in lowered, \
        "the un-deployed default feed must be stated plainly"
    assert "source_unavailable" in lowered, \
        "the default-route failure mode must be documented"
    dashboard = _paragraph_with(section, "dashboard")
    assert "dashboard" in dashboard.lower() and "feed" in dashboard.lower(), \
        "the dashboard must be distinguished from the JSON feed"


def test_cache_fallback_is_not_documented_as_acquisition_success(tmp_path):
    """A last-known-good replay is `ok: true` WITH a `source` block, so neither
    field may be documented as proof that the acquisition succeeded (QUALITY 10a
    finding 3).

    Behaviour first: a real ``CatalogSource`` is refreshed, its file then
    vanishes, and the second refresh returns ``ok: true`` plus the taken-down
    snapshot along with ``refresh_failed``/``cache_fallback`` — the same shape
    the README's worked example prints. Then the prose is checked against that
    behaviour, not against a paraphrase of it.
    """
    from watchdog_catalog import source

    target = tmp_path / "lookup.json"
    target.write_bytes(FIXTURE.read_bytes())
    now = 1_700_000_000
    origin = source.CatalogSource("file", str(target),
                                  cache_dir=str(tmp_path / "cache"))
    assert origin.refresh(now=now)["ok"] is True
    target.unlink()

    fallback = origin.refresh(now=now + 5)
    assert fallback["ok"] is True, fallback
    assert isinstance(fallback["source"], dict), fallback["source"]
    assert {"refresh_failed", "cache_fallback"} <= \
        set(fallback["source"]["warnings"]), fallback["source"]["warnings"]
    assert fallback["source"]["cached"] is True, fallback["source"]

    paragraph = _paragraph_with(_plain(_section(_readme(), CONSUMER_HEADING)),
                                "source block")
    lowered = paragraph.lower()
    assert "ok: true" in lowered, \
        "the worked example no longer explains the ok:true/source pair"
    assert "validated snapshot was returned" in lowered, \
        "cached success must be described as a returned snapshot, not a success"
    for warning in ("refresh_failed", "cache_fallback"):
        assert warning in paragraph, \
            f"the cache-fallback caveat omits {warning!r}"
    assert "failed refresh" in lowered and "memory" in lowered, \
        "a last-known-good replay is not spelled out as a failed refresh"
    # The removed verbatim claim: ok+source proves the acquisition succeeded.
    assert "means the acquisition succeeded" not in lowered, \
        "the false acquisition-succeeded claim is back"


def test_metadata_filter_table_matches_real_query_results():
    """Every documented filter row is real output, and still discriminating.

    The table claims a progression — same text query, one filter added per row —
    so each row must equal the core's answer for exactly its arguments, and rows
    that differ only in filters must not collapse onto the same endpoints. That
    is what makes a wrong documented argument (or a wrong documented result)
    fail here instead of passing as prose.
    """
    snapshot = _snapshot()
    rows = _filter_table()
    assert len(rows) >= 4, f"the worked filter table lost its rows: {rows}"

    observed = []
    for arguments, documented in rows:
        real = _matches(snapshot, arguments)
        assert real == documented, (
            f"documented result for {arguments} is not real output: "
            f"{documented} != {real}")
        observed.append((arguments.get("query"), tuple(real)))

    for query in {query for query, _ in observed}:
        sets = [result for result_query, result in observed if result_query == query]
        assert len(set(sets)) == len(sets), (
            f"the filter rows for {query!r} no longer tell the filters apart: {sets}")

    # Sensitivity to the inclusive-threshold claim: the documented rows that
    # differ by a single context token must answer differently.
    boundaries = []
    for (first, _), (second, _) in itertools.combinations(rows, 2):
        if set(first) != set(second) or "min_context_length" not in first:
            continue
        if {k: v for k, v in first.items() if k != "min_context_length"} != \
                {k: v for k, v in second.items() if k != "min_context_length"}:
            continue
        if abs(first["min_context_length"] - second["min_context_length"]) == 1:
            boundaries.append((first, second))
    assert boundaries, "the one-token inclusive-boundary row pair is gone"
    for first, second in boundaries:
        assert _matches(snapshot, first) != _matches(snapshot, second), (
            "a one-token threshold change did not change the documented result")


def test_paged_search_example_re_derives_with_page_scoped_counts():
    """The paged example is real output, and paging narrows the page only.

    ``total_matches`` is the unpaginated cardinality and ``counts`` describes
    the page, so the same query without a limit must still match
    ``total_matches`` endpoints while the page carries one row. The documented
    beyond-end offset is exercised too: still a success, empty page.
    """
    from watchdog_catalog import query

    snapshot = _snapshot()
    example = _paged_example()
    arguments = {"query": example["query"], "limit": example["limit"],
                 "offset": example["offset"]}
    real = query.search_models(snapshot, arguments)
    for key in ("ok", "query", "limit", "offset", "total_matches",
                "returned_count", "next_offset", "counts", "caveats"):
        assert example[key] == real[key], f"example {key} is not real output"
    assert example["returned_count"] == len(example["endpoints"]) == 1, example
    for documented, actual in zip(example["endpoints"], real["endpoints"]):
        for key, value in documented.items():
            assert actual[key] == value, \
                f"example endpoint {key} is not real output"

    unpaged = query.search_models(snapshot, {"query": example["query"]})
    assert unpaged["total_matches"] == example["total_matches"]
    assert unpaged["returned_count"] == unpaged["total_matches"]
    assert example["returned_count"] < unpaged["returned_count"]
    assert example["counts"]["endpoints"] == example["returned_count"]
    assert example["counts"]["endpoints"] < unpaged["counts"]["endpoints"]

    section = _section(_readme(), METADATA_HEADING)
    beyond_offset = re.search(r"`offset: (\d+)`", section)
    assert beyond_offset, "the beyond-end page example is gone"
    beyond = query.search_models(snapshot, {"query": example["query"],
                                           "offset": int(beyond_offset.group(1))})
    assert beyond["ok"] is True and beyond["endpoints"] == [], beyond
    assert beyond["returned_count"] == 0 and beyond["next_offset"] is None
    assert beyond["total_matches"] == example["total_matches"]
    assert beyond["counts"] == {"gateways": 0, "endpoints": 0,
                                "distinct_raw_ids": 0, "marker_group_labels": 0}


def test_min_context_is_inclusive_and_rejects_unknown_metadata():
    """Degrees of "no context": inclusive threshold, known zero, unknown.

    ``0`` keeps a known zero context and still excludes a ``null`` one, while an
    unfiltered search returns the unknown-context endpoint — unknown metadata
    fails the filter that tests it, not every search.
    """
    edge = _snapshot("metadata-edge-cases.json")

    assert _matches(edge, {"query": "reasoning", "min_context_length": 65536}) \
        == ["synthetic-a / vendor/known-context"]
    kept = _matches(edge, {"query": "reasoning", "min_context_length": 0})
    assert kept == ["synthetic-a / vendor/known-context",
                    "synthetic-a / vendor/zero-context"], kept
    assert _matches(edge, {"query": "reasoning"}) != kept

    bullet = [p for p in _bullets(METADATA_HEADING) if "min_context_length" in p]
    assert bullet, "the minimum-context guidance is gone"
    assert "inclusive" in bullet[0].lower(), \
        "the threshold is not documented as inclusive"
    assert "unknown" in bullet[0].lower(), \
        "unknown context is not documented as failing the filter"


def test_status_freshness_shapes_are_bound_to_the_fixture_clock():
    """Observation age, the inclusive cadence boundary and the three shapes.

    A fresh ``ok: true`` status is about the query, never about freshness; a
    null or future tick is reported as unknown/future and is never dressed up
    as a stale known observation.
    """
    from watchdog_catalog import query

    snapshot = _snapshot()
    tick, cadence = snapshot["tick_epoch"], snapshot["cadence_s"]

    at_tick = query.watchdog_status(snapshot, {}, now=tick)
    assert at_tick["last_tick_age_s"] == 0 and at_tick["tick_fresh"] is True
    assert at_tick["warnings"] == []
    boundary = query.watchdog_status(snapshot, {}, now=tick + cadence)
    assert boundary["last_tick_age_s"] == cadence
    assert boundary["tick_fresh"] is True, "age == cadence_s is still fresh"
    stale = query.watchdog_status(snapshot, {}, now=tick + cadence + 1)
    assert stale["tick_fresh"] is False and stale["warnings"] == ["tick_stale"]
    future = query.watchdog_status(snapshot, {}, now=tick - 1)
    assert future["last_tick_age_s"] is None and future["tick_fresh"] is False
    assert future["warnings"] == ["tick_epoch_future"]

    unknown = query.watchdog_status(_snapshot("status-unknown-time.json"), {},
                                    now=1759200000)
    assert unknown["tick_epoch"] is None and unknown["last_tick_age_s"] is None
    assert unknown["tick_fresh"] is False
    assert "tick_epoch_unknown" in unknown["warnings"]
    assert "tick_stale" not in unknown["warnings"]

    for status in (at_tick, stale, future, unknown):
        assert status["ok"] is True


def test_cached_replay_is_not_documented_as_an_identical_source_block(tmp_path):
    """Behaviour first: a last-known-good replay keeps counting its ages.

    The successful read and the replay share an ``ok``/``source`` shape but not
    the block itself, so the prose must not claim the identical block that
    induced the 10a cache finding. ``fetched_at`` and ``observation_age_s`` are
    carried forward, which is exactly what distinguishes provenance from a
    fresh acquisition.
    """
    from watchdog_catalog import source

    target = tmp_path / "lookup.json"
    target.write_bytes(FIXTURE.read_bytes())
    now = 1759201000
    origin = source.CatalogSource("file", str(target),
                                  cache_dir=str(tmp_path / "cache"))
    fresh = origin.refresh(now=now)
    assert fresh["ok"] is True, fresh
    assert fresh["source"]["cached"] is False
    assert fresh["source"]["fetch_age_s"] == 0

    target.unlink()
    replay = origin.refresh(now=now + 900)
    assert replay["ok"] is True, replay
    assert replay["source"]["cached"] is True
    assert replay["source"]["not_modified"] is False
    assert {"refresh_failed", "cache_fallback"} <= set(replay["source"]["warnings"])
    assert replay["source"] != fresh["source"], \
        "a replay is not a byte-identical source block"
    assert replay["source"]["fetched_at"] == fresh["source"]["fetched_at"]
    assert replay["source"]["fetch_age_s"] == 900
    assert replay["source"]["observation_age_s"] \
        == fresh["source"]["observation_age_s"] + 900

    paragraph = _paragraph_with(_plain(_section(_readme(), CONSUMER_HEADING)),
                                "source block")
    lowered = paragraph.lower()
    assert "the same source block" not in lowered, \
        "the identical-source-block claim is back"
    assert "not the same block" in lowered, \
        "the replay's differing source block is not spelled out"
    assert "source.cached" in paragraph and "cache_fallback" in paragraph


def test_published_operator_fields_are_documented_as_unobservable():
    """Operator state a published snapshot cannot see stays documented as null."""
    from watchdog_catalog import query

    status = query.watchdog_status(_snapshot(), {}, now=1759200000)
    for field in ("pending_alerts", "site_published", "site_publish_age_s"):
        assert status[field] is None, field

    bullet = [p for p in _bullets(CANNOT_HEADING) if "pending_alerts" in p]
    assert bullet, "the unobservable-operator-fields caveat is gone"
    for field in ("pending_alerts", "site_published", "site_publish_age_s"):
        assert field in bullet[0], field
    assert "null" in bullet[0].lower()


def test_count_scopes_are_documented_as_distinct():
    """A page count, a whole-catalogue count and a raw-ID count are not the same
    number, and the guidance must not let a reader conflate them."""
    from watchdog_catalog import query

    snapshot = _snapshot()
    page = query.search_models(snapshot, {"query": "model", "limit": 1})
    whole = query.watchdog_status(snapshot, {}, now=1759200000)
    endpoints = query.list_endpoints(snapshot, {})
    exported = query.list_free_models(snapshot, {})

    assert page["counts"]["endpoints"] == page["returned_count"] == 1
    assert whole["counts"]["endpoints"] > page["counts"]["endpoints"]
    assert whole["counts"]["gateways"] == 3, "the zero-ID gateway is excluded"
    assert endpoints["counts"]["models"] == endpoints["counts"]["marker_group_labels"]
    distinct_raw_ids = {raw for provider in snapshot["provider_order"]
                        for raw in snapshot["providers"][provider]["model_ids"]}
    assert exported["total_ids"] == len(distinct_raw_ids)
    assert exported["total_ids"] < whole["counts"]["endpoints"], \
        "raw ids and endpoint pairs must not be the same number here"

    bullet = [p for p in _bullets(CANNOT_HEADING) if "total_ids" in p]
    assert bullet, "the count-scope guidance is gone"
    for token in ("page-scoped", "whole-catalogue", "total_ids"):
        assert token in bullet[0], token


def test_unknown_metadata_is_documented_as_distinct_from_known_zero_and_empty():
    """A known ``0``/``[]`` is data; ``null`` is silence, and ``unknown_fields``
    says which fields are silent."""
    from watchdog_catalog import query

    snapshot = _snapshot("capability-unknown.json")
    rows = {row["model_id"]: row for row in
            query.search_models(snapshot, {"limit": 200})["endpoints"]}
    known = rows["vendor/known-empty-capabilities"]
    unknown = rows["vendor/unknown-capabilities"]

    assert known["context_length"] == 0 and known["input_modalities"] == []
    assert known["unknown_fields"] == [], "a known zero/[] must not read as unknown"
    assert unknown["context_length"] is None and unknown["input_modalities"] is None
    assert unknown["unknown_fields"] == ["context_length", "input_modalities",
                                         "output_modalities",
                                         "supported_parameters"]

    bullet = [p for p in _bullets(CANNOT_HEADING) if "unknown_fields" in p]
    assert bullet, "the unknown-versus-zero guidance is gone"
    assert "unknown is not zero" in bullet[0].lower()


def test_quick_start_anchor_keeps_the_baseline_deep_link():
    """The operator quick start may be re-routed under the consumer opening, but
    its `#quick-start` anchor must survive (QUALITY 10a finding 1).

    The baseline README exposed ``## Quick start``; the consumer-first rewrite
    moved that material into the self-hosting section. External deep links into
    ``#quick-start`` are outside this repo's control, so the slug is pinned and
    the off-scope qualifier must live in prose rather than inside the heading,
    where it would silently change the slug.
    """
    text = _readme()
    assert "quick-start" in _heading_slugs(text), \
        "the baseline #quick-start anchor no longer resolves"
    assert "\n### Quick start\n" in text, \
        "the operator quick start heading text is not exactly 'Quick start'"
    assert "Quick start (self-hosting)" not in text, \
        "the scope qualifier is back inside the heading, which breaks the slug"
    # It still routes the operator, and it still sits below the consumer hand-off.
    assert text.index("\n### Quick start\n") > text.index(OPERATOR_HEADING), \
        "the quick start drifted out of the self-hosting section"
    quick_start = _section(text, "\n### Quick start\n")
    assert "self-hosting" in quick_start.lower(), \
        "the self-hosting qualifier is missing from the quick start prose"


def test_every_internal_readme_anchor_resolves():
    """Links are checked, not assumed: every `](#anchor)` must match a heading."""
    text = _readme()
    slugs = _heading_slugs(text)
    targets = set(re.findall(r"\]\(#([^)]+)\)", text))
    assert targets, "no internal anchors found"
    missing = sorted(targets - slugs)
    assert not missing, f"README anchors with no heading: {missing}"


def test_readme_json_blocks_are_valid_json():
    """Every fenced JSON block parses; a broken example is a broken README."""
    blocks = [body for language, body in _fenced_languages(_readme())
              if language == "json"]
    assert blocks, "README lost its fenced JSON examples"
    for body in blocks:
        json.loads(body)


def test_every_shell_example_parses():
    """Shell snippets are parsed, not eyeballed — including the install block.

    `bash -n` catches quoting and continuation drift in every operator and
    consumer example, not just the cron wrapper it was originally written for.
    """
    blocks = [body for language, body in _fenced_languages(_readme())
              if language == "bash"]
    assert blocks, "README lost its fenced shell examples"
    for body in blocks:
        proc = subprocess.run(["bash", "-n"], input=body, text=True,
                              capture_output=True)
        assert proc.returncode == 0, (
            f"shell example is not valid bash:\n{body}\n{proc.stderr}")


def test_cron_wrapper_exempts_routine_exit_1():
    """The wrapper must stay SILENT on bare exit 1 (routine partial outage —
    the stale line in the alert explains why) and route every other failure to
    stderr with a non-zero exit. Cron stdout must NEVER become a page channel."""
    text = _readme()
    assert "-eq 1 ] ||" in text, "README cron wrapper lost the exit-1 exemption"
    assert "c=$?" in text, "wrapper must capture the real exit code into c"
    assert '>&2' in text, "failure echoes must go to stderr, not stdout"
    # delivery topology stated plainly: webhook delivers, cron is local-only
    assert "--deliver local" in text, "cron registration must be silent (--deliver local)"
    assert "exit 1" in text and "routine" in text.lower()


def test_cron_wrapper_guards_cd_before_python():
    """Fix-round-4 #1: a failed `cd` must PAGE, never fall into the exit-1
    exemption — under bash a failed `cd` IS exit 1, so chaining both stages
    into one exemption clause went permanently silent the moment the project
    dir moved/renamed (exactly the dead-monitor-indistinguishable-from-silence
    scenario). The documented wrapper must keep a separate cd-guard stage that
    runs BEFORE the python stage."""
    blocks = _fenced_blocks(_readme())
    wrapper = next(b for b in blocks if "cannot cd" in b)
    lines = [l for l in wrapper.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    cd_idx = next(i for i, l in enumerate(lines) if "cannot cd" in l)
    py_idx = next(i for i, l in enumerate(lines) if "python3 inference_watchdog.py" in l)
    assert cd_idx < py_idx, "cd-guard must run BEFORE the python invocation"
    assert "exit 1" in lines[cd_idx], "cd-guard must page (exit non-zero), not fall through"


def test_readme_wrapper_block_is_wellformed_shell():
    """The documented wrapper block must parse as valid shell under bash -n —
    guards against quoting drift in the README example."""
    blocks = _fenced_blocks(_readme())
    wrapper = next(b for b in blocks if "cannot cd" in b)
    proc = subprocess.run(["bash", "-n"], input=wrapper, text=True, capture_output=True)
    assert proc.returncode == 0, f"wrapper block is not valid shell:\n{proc.stderr}"
    assert "inference_watchdog.py" in wrapper
    assert "FAILED" in wrapper


def test_no_invalid_cron_tokens_in_readme():
    """Regression pin: this build has no `hermes cron register` subcommand and
    no `discord-home` deliver token; neither string may reappear anywhere in
    the README (the old registration example used both)."""
    text = _readme()
    assert "cron register" not in text, \
        "invalid subcommand 'hermes cron register' back in README"
    assert "discord-home" not in text, \
        "invalid deliver token 'discord-home' back in README"


def test_documented_create_command_is_silent_script_mode():
    """The documented `hermes cron create` invocation must be script-mode
    (--script + --no-agent) and SILENT (--deliver local) — the webhook in
    ~/.hermes/.env is the only alert path, never cron delivery."""
    blocks = _fenced_blocks(_readme())
    cmd_block = next(b for b in blocks if "hermes cron create" in b)
    joined = " ".join(cmd_block.split())
    assert "--script" in joined, "create command must use script mode (--script)"
    assert "--no-agent" in joined, "create command must skip the LLM (--no-agent)"
    assert "--deliver local" in joined, "cron job must be silent (--deliver local)"


def test_readme_init_documents_bak_overwrite():
    """Fix-round-5 #5 (cosmetic): the --init section must state that each
    successful init OVERWRITES a prior roster.json.bak — the archive-site code
    comment says '(documented in README)', so the README must actually say it."""
    text = _readme()
    assert "roster.json.bak" in text, "--init section lost the .bak mention"
    assert "overwrit" in text.lower(), ".bak overwrite behavior not documented"
