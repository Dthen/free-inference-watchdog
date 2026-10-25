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
an identical `source` block (the 10a cache finding). Count scopes, the operator
fields a published snapshot cannot observe and the documented marker claims
are pinned the same way: the caveat sentence is bound to the tools that
actually return `caveats`, and the documented strip/no-strip ids, the "one
label" result and the both-ends clause are read out of the guidance and
checked against the shipped stripper's real output.

Operator route (F-R2-4, unit 10b2): delivery topology (operator decision,
2026-08-26) is unchanged — the Discord webhook in the project-local `.env` is
the ONLY alert path and the cron job is silent (`--deliver local`). The
wrappers' job is diagnosability, not paging, and their failure contract is
EXECUTED below rather than read: the documented fenced bodies are run under
real bash against a harmless monitor stub, so "routine exit 1 stays silent,
anything else diagnoses on STDERR and keeps its own status, and a failed `cd`
never launches the monitor" is measured per stage. The registration vectors are
run the same way against an argv-recording `hermes` stub, and `hermes cron
register` / `discord-home` — both invalid forms that regressed here once
already — are still pinned absent.
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
    """The individual list items of ONE ``###`` subsection, as prose.

    Items are split apart even when the README writes the list without blank
    lines between them, and fenced examples are dropped. Both matter: a whole
    10-item block let an unrelated freshness bullet satisfy "the operator
    fields say null" (QUALITY 10b1 finding 2), and an example must never
    satisfy a claim that belongs to the guidance list.
    """
    text = _readme()
    body = text[text.index(heading) + len(heading):]
    for stop in ("\n### ", "\n## "):
        end = body.find(stop)
        if end != -1:
            body = body[:end]
    body = re.sub(r"```.*?```", "", body, flags=re.S)

    items, current = [], []

    def flush():
        if current:
            items.append(_plain(" ".join(" ".join(current).split())))
            current.clear()

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("- "):
            flush()
            current.append(stripped[2:])
        elif current:
            current.append(stripped)
    flush()
    return [item for item in items if item]


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


def test_absence_caveat_is_scoped_to_the_tools_that_carry_it():
    """The caveat ships on ``get_model`` and ``search_models`` only.

    QUALITY 10b1 finding 1: "every payload carries the absence caveat" was
    false — ``list_endpoints``, ``list_free_models`` and ``watchdog_status``
    return no ``caveats`` key. The claim is bound to the real payloads here, so
    widening it back to a universal claim fails.
    """
    from watchdog_catalog import query

    snapshot = _snapshot()
    payloads = {
        "get_model": query.get_model(snapshot, {"model_id": "vendor/shared"}),
        "search_models": query.search_models(snapshot, {}),
        "list_endpoints": query.list_endpoints(snapshot, {}),
        "list_free_models": query.list_free_models(snapshot, {}),
        "watchdog_status": query.watchdog_status(snapshot, {}, now=1759200000),
    }
    carriers = {name for name, payload in payloads.items() if "caveats" in payload}
    assert carriers == {"get_model", "search_models"}, carriers
    caveat = payloads["get_model"]["caveats"]
    assert caveat and caveat == payloads["search_models"]["caveats"]

    consumer = _section(_readme(), CONSUMER_HEADING)
    mentions = [p for p in _paragraphs(_plain(consumer))
                if "absence caveat" in p.lower() and not p.startswith("- ")]
    mentions += [item for item in _bullets(CANNOT_HEADING)
                 if "caveat" in item.lower()]
    assert len(mentions) >= 2, f"the absence-caveat guidance is gone: {mentions}"

    named = set()
    for paragraph in mentions:
        lowered = paragraph.lower()
        for universal in ("every payload", "every result"):
            assert universal not in lowered, (
                f"the false universal caveat claim is back: {paragraph}")
        tools = {name for name in payloads if name in paragraph}
        assert tools and tools <= carriers, (
            f"a caveat claim names a tool that carries no caveats: {paragraph}")
        named |= tools
    assert named == carriers, "both caveat-carrying tools must be named explicitly"


def test_published_operator_fields_are_documented_as_unobservable():
    """Operator state a published snapshot cannot see stays documented as null."""
    from watchdog_catalog import query

    status = query.watchdog_status(_snapshot(), {}, now=1759200000)
    for field in ("pending_alerts", "site_published", "site_publish_age_s"):
        assert status[field] is None, field

    bullet = [p for p in _bullets(CANNOT_HEADING) if "pending_alerts" in p]
    assert len(bullet) == 1, f"operator fields ride other bullets: {bullet}"
    for field in ("pending_alerts", "site_published", "site_publish_age_s"):
        assert field in bullet[0], field
    # One list item, so an unrelated freshness "null" cannot satisfy this.
    assert "as null" in bullet[0].lower(), \
        "the operator fields are no longer documented as null"


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


def test_documented_marker_examples_match_the_real_stripper():
    """The documented strip examples are stated results, so they are executed.

    SPEC 10b1 R3: running the shipped stripper is not the same as pinning what
    the README says it does. The documented ids, the ``contribute one label``
    result and the retained ``both ends strip`` clause are read out of the
    guidance and checked against real ``strip_free_marker`` output, so
    "contribute two labels" or "only the start strips" cannot stay green.
    """
    from watchdog_catalog import query

    strip = query.strip_free_marker
    assert strip("vendor/model:free") == "vendor/model"
    assert strip("free-vendor-model") == "vendor-model"
    assert strip("vendor/freetier") == "vendor/freetier"
    assert strip("free-vendor-model_free") == "vendor-model", "both ends strip"
    assert strip("free-") == "free-", "an emptied id must be returned unchanged"

    snapshot = _snapshot()
    served = {model_id for provider in snapshot["provider_order"]
              for model_id in snapshot["providers"][provider]["model_ids"]}

    # The documented one-label example is a stated result, not just an
    # executed one: read its ids and its count out of the metadata guidance and
    # let the stripper that ``counts.marker_group_labels`` uses answer it.
    label_bullets = [item for item in _bullets(METADATA_HEADING)
                     if "marker_group_labels" in item]
    assert len(label_bullets) == 1, label_bullets
    label_claim = label_bullets[0]
    documented_ids = re.findall(r"\b[\w.\-]+/[\w.\-:]+\b", label_claim)
    assert len(documented_ids) == 2, \
        f"the documented label example names {documented_ids}"
    assert set(documented_ids) <= served, \
        f"the documented ids are not fixture endpoints: {documented_ids}"
    documented_result = re.search(r"contribute (\w+) labels? between them",
                                  label_claim)
    assert documented_result, "the documented one-label result is gone"
    label_count = {"one": 1, "two": 2, "three": 3}.get(
        documented_result.group(1).lower())
    labels = {strip(model_id) for model_id in documented_ids}
    assert label_count == len(labels) == 1, \
        f"README says {documented_result.group(0)!r} for {documented_ids}, " \
        f"which strip to {sorted(labels)}"

    rows = query.search_models(snapshot, {"query": "model", "limit": 200})
    page_labels = {strip(row["model_id"]) for row in rows["endpoints"]}
    assert {"vendor/model", "vendor/model-renamed"} <= page_labels
    assert rows["counts"]["marker_group_labels"] == len(page_labels)
    assert set(documented_ids) <= {row["model_id"] for row in rows["endpoints"]}

    # The guidance must still name those ids and say which of them strip; the
    # short clause pin catches an inverted "does not", with no whole-sentence
    # wording pin.
    marker_bullets = [item for item in _bullets(CANNOT_HEADING)
                      if "Marker lookups" in item]
    assert len(marker_bullets) == 1, marker_bullets
    marker = marker_bullets[0]
    examples = marker.split("\u2014", 1)[1]
    assert "vendor/model:free and free-vendor-model strip" in examples
    assert "vendor/freetier does not" in examples

    # The retained "both ends strip" clause is a stated result too. Build its
    # id from the two documented examples — the start-marked id plus the end
    # marker the other one carries — and require the README's claim to agree
    # with what ONE strip of that id actually does.
    both = re.match(r"\s*(\S+) and (\S+) strip", examples)
    assert both, "the documented strip examples are gone"
    end_marked, start_marked = both.group(1), both.group(2)
    end_marker = end_marked[len(strip(end_marked)):]
    assert end_marker, end_marked
    both_ends = start_marked + end_marker
    both_stripped = strip(both_ends)
    start_only = strip(start_marked) + end_marker
    claims_both = "both ends strip" in marker.lower()
    assert claims_both or "only the start" in marker.lower(), \
        "the documented both-ends result is gone"
    assert claims_both == (both_stripped != start_only), \
        f"README claims both ends strip={claims_both}, but " \
        f"{both_ends!r} strips to {both_stripped!r}"
    assert both_stripped == strip(start_marked), \
        f"{both_ends!r} must land in the same label group as " \
        f"{start_marked!r}"


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


# -- operator wrappers and registration, EXECUTED (unit 10b2) ----------------
#
# The self-hosting wrappers document a failure contract, so the fenced bodies are
# read out of the README and RUN under real bash against a harmless monitor stub:
# missing checkout, success, routine exit 1, fatal exit 2 and one representative
# other status, checking argv, return code and both streams each time. The only
# edit applied is the documented one — the two `EDIT ME` assignment lines are
# re-pointed at an owned checkout and the stub — and their presence is asserted
# first, so a stale placeholder fails loudly instead of quietly exercising a
# body nobody documented. Nothing real runs here: no monitor, no resolver, no
# provider probe, no webhook, no lock, no Hermes job.

WRAPPER_CHECKOUT_LINE = 'WATCHDOG_HOME="$HOME/projects/free-inference-watchdog"'
WRAPPER_INTERPRETER_LINE = 'PYTHON="python3"'
TICK_WRAPPER = "inference-watchdog-tick.sh"
RESOLVE_WRAPPER = "inference-watchdog-resolve.sh"
ROUTINE_STATUS = 1   # documented routine partial-outage status
FATAL_STATUS = 2     # documented fatal status
OTHER_STATUS = 7     # any other status must keep its own code

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or sys.platform.startswith("win"),
    reason="the documented wrappers are POSIX bash; Windows is out of scope",
)

MONITOR_STUB = """#!/usr/bin/env bash
# Harmless monitor stand-in. Records its argv and exits with the requested
# status; it never reads the repository, state/, providers or the network.
printf '%s\\n' "$@" >> "$STUB_WITNESS"
exit "${STUB_STATUS:-0}"
"""

HERMES_STUB = """#!/usr/bin/env bash
# Harmless `hermes` stand-in: records argv, creates and registers nothing.
printf '%s\\n' "$@" > "$CRON_WITNESS"
"""


def _bash_blocks():
    """Every fenced bash block in the README, in document order."""
    return [body for language, body in _fenced_languages(_readme())
            if language == "bash"]


def _wrapper_body(script):
    """The one documented wrapper body for ``script``.

    The body names itself on its own comment line, so the registration block
    that merely mentions the same filename cannot satisfy this.
    """
    matches = [body for body in _bash_blocks() if f"\n# {script}" in body]
    assert len(matches) == 1, (
        f"expected exactly one documented body for {script}, found {len(matches)}")
    return matches[0]


def _target_wrapper(body, checkout, interpreter):
    """Apply ONLY the two documented `EDIT ME` edits to a wrapper body."""
    for line in (WRAPPER_CHECKOUT_LINE, WRAPPER_INTERPRETER_LINE):
        assert body.count(line) == 1, (
            f"the documented wrapper assignment {line!r} changed shape; the "
            "harness must not guess at a replacement")
    return body.replace(
        WRAPPER_CHECKOUT_LINE, f'WATCHDOG_HOME="{checkout}"').replace(
        WRAPPER_INTERPRETER_LINE, f'PYTHON="{interpreter}"')


def _execute_wrapper(body, workdir, checkout, status):
    """Run a documented wrapper body against the stub monitor, isolated."""
    workdir.mkdir(parents=True, exist_ok=True)
    stub = workdir / "monitor-stub.sh"
    stub.write_text(MONITOR_STUB, encoding="utf-8")
    stub.chmod(0o755)
    script = workdir / "wrapper.sh"
    script.write_text(_target_wrapper(body, checkout, stub), encoding="utf-8")
    witness = workdir / "monitor-argv.txt"
    (workdir / "home").mkdir()
    (workdir / "tmp").mkdir()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(workdir / "home"),
        "TMPDIR": str(workdir / "tmp"),
        "STUB_WITNESS": str(witness),
        "STUB_STATUS": str(status),
    }
    completed = subprocess.run(["bash", str(script)], env=environment,
                               text=True, capture_output=True, timeout=60)
    return completed, witness


def _argv_of(witness):
    return witness.read_text(encoding="utf-8").splitlines()


def _assert_wrapper_contract(body, expected_args, workdir):
    """The documented two-stage contract, measured on one wrapper body.

    ``workdir`` is owned scratch; every scenario gets its own subdirectory, so a
    witness file can only exist if that scenario really launched the monitor.
    """
    checkout = workdir / "checkout with spaces"
    checkout.mkdir(parents=True)

    completed, witness = _execute_wrapper(body, workdir / "success", checkout, 0)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "", f"wrapper wrote to stdout: {completed.stdout!r}"
    assert completed.stderr == "", f"success must be silent: {completed.stderr!r}"
    assert _argv_of(witness) == expected_args, (
        f"monitor ran with {_argv_of(witness)}, documented {expected_args}")

    completed, witness = _execute_wrapper(
        body, workdir / "routine", checkout, ROUTINE_STATUS)
    assert _argv_of(witness) == expected_args, (
        f"routine tick ran with {_argv_of(witness)}, documented {expected_args}")
    assert completed.returncode == 0, (
        f"routine partial outage (exit {ROUTINE_STATUS}) must be exempted, got "
        f"{completed.returncode}")
    assert completed.stdout == "", (
        f"routine exit 1 wrote to stdout: {completed.stdout!r}")
    assert completed.stderr == "", (
        f"routine exit 1 must add no wrapper output: {completed.stderr!r}")

    for status in (FATAL_STATUS, OTHER_STATUS):
        completed, witness = _execute_wrapper(
            body, workdir / f"status-{status}", checkout, status)
        assert _argv_of(witness) == expected_args, (
            f"exit {status} ran with {_argv_of(witness)}, documented "
            f"{expected_args}")
        assert completed.returncode == status, (
            f"wrapper changed the monitor status {status} to "
            f"{completed.returncode}")
        assert completed.stdout == "", (
            f"exit {status} wrote to stdout: {completed.stdout!r}")
        assert f"FAILED (exit {status})" in completed.stderr, (
            f"exit {status} must diagnose on stderr: {completed.stderr!r}")

    missing = workdir / "checkout-that-does-not-exist"
    completed, witness = _execute_wrapper(body, workdir / "missing", missing, 0)
    assert not witness.exists(), "the monitor ran from a nonexistent checkout"
    assert completed.returncode != 0, (
        f"a failed cd reported success (exit {completed.returncode}); a moved "
        "checkout would stay silent forever")
    assert completed.stdout == "", (
        f"a failed cd wrote to stdout: {completed.stdout!r}")
    assert "cannot cd" in completed.stderr, (
        f"a failed cd must diagnose on stderr: {completed.stderr!r}")
    assert str(missing) in completed.stderr, (
        "the cd diagnostic does not name the directory that is missing")


def _normalized_body(body):
    """Executable lines only, with the resolver's documented difference folded.

    Pins the README's claim that `--resolve` is the ONLY difference between the
    two wrappers: comments drop out, and the flag plus its diagnostic label are
    removed before the surviving lines are compared.
    """
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.replace("inference-watchdog resolve",
                                    "inference-watchdog").replace(" --resolve", "")
        lines.append(" ".join(stripped.split()))
    return lines


def _collapse_stages(body):
    """The historical regression: ONE clause guarding `cd` AND the monitor.

    Built out of the documented lines rather than authored separately, so the
    mutation tracks whatever the README says instead of drifting from it.
    """
    lines = body.splitlines()
    cd_index = next(i for i, line in enumerate(lines)
                    if line.strip().startswith('cd "$WATCHDOG_HOME"'))
    monitor_index = next(i for i, line in enumerate(lines)
                         if '"$PYTHON" inference_watchdog.py' in line)
    assert cd_index < monitor_index, "the cd guard does not precede the monitor"
    collapsed = []
    for index, line in enumerate(lines):
        if index == cd_index:
            collapsed.append(
                'cd "$WATCHDOG_HOME" && ' + lines[monitor_index].strip())
        elif index == monitor_index:
            continue
        else:
            collapsed.append(line)
    return "\n".join(collapsed) + "\n"


@POSIX_ONLY
def test_documented_tick_wrapper_enforces_both_failure_stages(tmp_path):
    """The tick wrapper is EXECUTED: missing checkout never launches the
    monitor; success and the routine exit 1 stay silent; 2 and a representative
    other status keep their own code with a stderr note and no stdout."""
    _assert_wrapper_contract(_wrapper_body(TICK_WRAPPER),
                             ["inference_watchdog.py"], tmp_path)


@POSIX_ONLY
def test_documented_resolve_wrapper_is_the_tick_wrapper_plus_resolve(tmp_path):
    """The resolver is documented as its own complete body, not as "same
    pattern as above": it runs `inference_watchdog.py --resolve` under the same
    two stages, and `--resolve` really is the only executable difference."""
    _assert_wrapper_contract(_wrapper_body(RESOLVE_WRAPPER),
                             ["inference_watchdog.py", "--resolve"], tmp_path)
    assert _normalized_body(_wrapper_body(RESOLVE_WRAPPER)) \
        == _normalized_body(_wrapper_body(TICK_WRAPPER)), \
        "the resolver wrapper drifted from the tick wrapper by more than --resolve"


@POSIX_ONLY
def test_wrapper_contract_rejects_the_known_wrapper_regressions(tmp_path):
    """Sensitivity proof: every mutation below must be caught by a BEHAVIOURAL
    assertion, not by a wording guard on the README text."""
    body = _wrapper_body(TICK_WRAPPER)
    expected = ["inference_watchdog.py"]

    collapsed = _collapse_stages(body)
    assert collapsed != body, "the collapsed-stage mutation did not apply"
    with pytest.raises(AssertionError, match="moved checkout would stay silent"):
        _assert_wrapper_contract(collapsed, expected, tmp_path / "collapsed")

    unguarded = body.replace(
        ' || { echo "inference-watchdog FAILED (cannot cd to $WATCHDOG_HOME)"'
        ' >&2; exit 1; }', "")
    assert unguarded != body, "the unguarded-cd mutation did not apply"
    with pytest.raises(AssertionError,
                       match="ran from a nonexistent checkout"):
        _assert_wrapper_contract(unguarded, expected, tmp_path / "unguarded")

    noisy = body.replace('[ "$c" -eq 1 ] || ', "")
    assert noisy != body, "the exit-1-exemption removal did not apply"
    with pytest.raises(AssertionError, match="must be exempted"):
        _assert_wrapper_contract(noisy, expected, tmp_path / "noisy")

    swallowed = body.replace('exit "$c"', "exit 0")
    assert swallowed != body, "the status-swallowing mutation did not apply"
    with pytest.raises(AssertionError, match="changed the monitor status"):
        _assert_wrapper_contract(swallowed, expected, tmp_path / "swallowed")

    wrong_args = body.replace("inference_watchdog.py",
                              "inference_watchdog.py --resolve")
    assert wrong_args != body, "the wrong-argv mutation did not apply"
    with pytest.raises(AssertionError, match="documented"):
        _assert_wrapper_contract(wrong_args, expected, tmp_path / "wrong-args")


@POSIX_ONLY
def test_wrappers_keep_one_delivery_path_and_never_write_stdout():
    """Both wrappers diagnose on stderr only and cannot page: the project-local
    `.env` webhook stays the single alert path, and script-mode stdout — which
    the scheduler would deliver verbatim — stays empty."""
    for script in (TICK_WRAPPER, RESOLVE_WRAPPER):
        body = _wrapper_body(script)
        lowered = body.lower()
        for banned in ("curl", "wget", "http", "webhook", "discord"):
            assert banned not in lowered, \
                f"{script}: a second delivery path appeared ({banned})"
        diagnostics = [line.strip() for line in body.splitlines()
                       if "echo" in line and not line.strip().startswith("#")]
        assert diagnostics, f"{script}: no failure diagnostics documented"
        for line in diagnostics:
            assert ">&2" in line, \
                f"{script}: a diagnostic can reach stdout: {line}"


def _registration_block(script):
    """The one `hermes cron create` block that registers ``script``."""
    matches = [body for body in _bash_blocks()
               if "hermes cron create" in body and script in body]
    assert len(matches) == 1, (
        f"expected exactly one registration block for {script}, found {len(matches)}")
    return matches[0]


def _run_registration(block, workdir):
    """Run a registration block with an argv-recording `hermes` first on PATH."""
    workdir.mkdir(parents=True, exist_ok=True)
    bin_dir = workdir / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "hermes"
    stub.write_text(HERMES_STUB, encoding="utf-8")
    stub.chmod(0o755)
    witness = workdir / "hermes-argv.txt"
    (workdir / "home").mkdir()
    (workdir / "tmp").mkdir()
    environment = {
        "PATH": os.pathsep.join(
            [str(bin_dir),
             os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")]),
        "HOME": str(workdir / "home"),
        "TMPDIR": str(workdir / "tmp"),
        "CRON_WITNESS": str(witness),
    }
    completed = subprocess.run(["bash", "-c", block], env=environment,
                               text=True, capture_output=True, timeout=60)
    return completed, witness


def _split_argv(argv):
    """Split a recorded command line into ``(positionals, options)``."""
    options, positionals, index = {}, [], 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            following = argv[index + 1] if index + 1 < len(argv) else None
            if following is not None and not following.startswith("--"):
                options[token] = following
                index += 2
                continue
            options[token] = True
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals, options


def _assert_registration_contract(block, script, schedule, workdir):
    """One documented `hermes cron create` vector, executed and inspected."""
    completed, witness = _run_registration(block, workdir)
    assert completed.returncode == 0, completed.stderr
    assert witness.exists(), "the registration block never invoked `hermes`"
    positionals, options = _split_argv(_argv_of(witness))
    assert positionals[:2] == ["cron", "create"], positionals
    assert positionals[2:] == [schedule], (
        f"documented schedule is {positionals[2:]!r}, expected [{schedule!r}]")
    assert options.get("--name") == script.removesuffix(".sh"), options
    assert options.get("--script") == script, (
        "registration must pass the bare script file name, got "
        f"{options.get('--script')!r}")
    assert os.sep not in options["--script"], "the script path must be bare"
    assert options.get("--no-agent") is True, "script mode must skip the LLM"
    assert options.get("--deliver") == "local", (
        f"cron delivery must stay local, got {options.get('--deliver')!r}")


@POSIX_ONLY
def test_documented_registration_vectors_are_silent_script_jobs(tmp_path):
    """Both existing schedules are executed against an argv-recording stub: the
    tick on `17 */1 * * *`, the resolver on `*/15 * * * *`, each registering the
    documented bare script with `--no-agent --deliver local`."""
    _assert_registration_contract(_registration_block(TICK_WRAPPER),
                                  TICK_WRAPPER, "17 */1 * * *",
                                  tmp_path / "tick")
    _assert_registration_contract(_registration_block(RESOLVE_WRAPPER),
                                  RESOLVE_WRAPPER, "*/15 * * * *",
                                  tmp_path / "resolve")


@POSIX_ONLY
def test_registration_contract_rejects_the_known_registration_regressions(tmp_path):
    """Sensitivity proof for the registration vector: schedule drift, waking the
    LLM, an invalid delivery token and an absolute script path each reach a
    behavioural argv assertion."""
    block = _registration_block(TICK_WRAPPER)
    regressions = {
        "schedule drift": (block.replace('"17 */1 * * *"', '"0 * * * *"'),
                           "documented schedule"),
        "agent mode": (block.replace("  --no-agent \\\n", ""), "script mode"),
        "invalid deliver token": (
            block.replace("--deliver local", "--deliver discord-home"),
            "cron delivery"),
        "absolute script path": (
            block.replace("--script inference-watchdog-tick.sh",
                          "--script /opt/scripts/inference-watchdog-tick.sh"),
            "bare script file name"),
    }
    for label, (mutated, message) in regressions.items():
        assert mutated != block, f"{label}: the mutation did not apply"
        with pytest.raises(AssertionError, match=message):
            _assert_registration_contract(
                mutated, TICK_WRAPPER, "17 */1 * * *",
                tmp_path / label.replace(" ", "-"))


def test_self_hosting_examples_are_portable_and_scheduler_optional():
    """Operator examples must work off this one machine and must not read as a
    consumer prerequisite: an editable quoted checkout path (spaces included) in
    both the quick start and the wrappers, a script location under the active
    Hermes home, and wrapper-local settings rather than scheduler-inherited
    environment."""
    text = _readme()
    assert "/home/kimbo" not in text, \
        "an operator example is still pinned to one machine's home directory"

    quick_start = " ".join(_section(text, "\n### Quick start\n").split())
    assert WRAPPER_CHECKOUT_LINE in quick_start, \
        "the quick start lost its editable, quoted checkout assignment"
    assert 'cd "$WATCHDOG_HOME" || exit 1' in quick_start, \
        "the quick start no longer selects the checkout directory"
    assert "spaces" in quick_start.lower(), "the spaces caveat is missing"
    assert "not a watchdog option" in quick_start.lower(), \
        "a shell path setting is presented as a watchdog CLI/API option"

    cron = " ".join(_section(text, "## Cron registration").split())
    assert "$HERMES_HOME/scripts/" in cron, \
        "the script location is not tied to the active Hermes home"
    assert "~/.hermes/scripts/" in cron, "the default Hermes home is unnamed"
    assert "sanitiz" in cron.lower(), \
        "wrapper-local configuration is not justified against scheduler env"
    assert "not a prerequisite" in cron.lower(), \
        "Hermes cron is not marked optional for consumers"
    for script in (TICK_WRAPPER, RESOLVE_WRAPPER):
        assert f"~/.hermes/scripts/{script}" in text, \
            f"{script} has no documented install location"


def test_no_invalid_cron_tokens_in_readme():
    """Regression pin: this build has no `hermes cron register` subcommand and
    no `discord-home` deliver token; neither string may reappear anywhere in
    the README (the old registration example used both)."""
    text = _readme()
    assert "cron register" not in text, \
        "invalid subcommand 'hermes cron register' back in README"
    assert "discord-home" not in text, \
        "invalid deliver token 'discord-home' back in README"


def test_readme_init_documents_bak_overwrite():
    """Fix-round-5 #5 (cosmetic): the --init section must state that each
    successful init OVERWRITES a prior roster.json.bak — the archive-site code
    comment says '(documented in README)', so the README must actually say it."""
    text = _readme()
    assert "roster.json.bak" in text, "--init section lost the .bak mention"
    assert "overwrit" in text.lower(), ".bak overwrite behavior not documented"
