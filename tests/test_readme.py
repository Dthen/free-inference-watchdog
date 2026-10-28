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
published JSON feed is the DEFAULT source (its raw GitHub URL, with the Pages
copy documented as a mirror) and that WebMCP is planned, never a remotely
connectable MCP HTTP URL. The obsolete "feed is not live yet" wording is
reproduced as a mutation the same predicate rejects, so the correction cannot
silently regress. That hand-off must not cost existing readers
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

Agent guidance (unit 10c): `AGENTS.md` explains the architecture that already
ships — the producer/consumer boundary, the `watchdog_catalog` module map, the
SDK boundary and the artifact/publication gate. Its one executable POINTER claim
is checked as a RESOURCE: every module or script AGENTS names must exist in this
checkout, with a planted stale name proving the check is sensitive rather than
vacuous. The two guidance claims parent verification found wrong are bound the
same way — observation time and the clock seams to `watchdog_status`'s injected
`now`, the async source's own seams and a raising-clock construction probe; the
packaged tool contract to the `inputSchema` objects `tools.json` actually ships
— and the obsolete sentences are planted as mutations those predicates reject.

Browser route (unit 10d): the README's WebMCP section documents a GENERATED
artifact, so it is executed rather than read. A synthetic roster is built into a
real page by the real builder, the two inert islands and the three inlined
classic scripts are read back out of that HTML, and the scripts are run in a
Node realm against a fake in-page host: five registrations, the three documented
annotations, `source: {mode: "browser"}` provenance on success AND argument
errors, and complete bulk output that is compared field-for-field with the
shared Python core over the page's own snapshot. Availability is pinned as a
SCOPE (development flag, Chrome 149+ origin trial, secure context, `tools`
Permissions Policy) with no universal claim, the oversize-output rule is pinned
as Chrome's RECOMMENDATION rather than a cap, the publication paragraph is
checked against the phase the tracked page is actually in (pre-publication and
freshly inlined both pass; a deployment claim the artifact contradicts, or a
caveat it has outgrown, does not), and the retired "planned / no adapter /
later units" claims are planted as mutations the same predicates reject.
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


def _client_configs():
    """Every documented generic stdio client block, in document order."""
    blocks = []
    for language, body in _fenced_languages(_readme()):
        if language != "json":
            continue
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict) and "mcpServers" in document:
            blocks.append(document)
    return blocks


def _server_entry(document):
    """The one ``command``/``args`` server entry inside a client block."""
    servers = document["mcpServers"]
    assert list(servers) == ["free-inference-watchdog"], servers
    entry = servers["free-inference-watchdog"]
    assert set(entry) == {"command", "args"}, entry
    return entry


def _documented_default_server():
    """The PRIMARY stdio block: the live default route, with no source flag."""
    configs = _client_configs()
    assert configs, "README has no fenced JSON stdio client block"
    return _server_entry(configs[0])


def _documented_preview_server():
    """The explicitly pinned SYNTHETIC-preview stdio block, if documented."""
    for document in _client_configs()[1:]:
        entry = _server_entry(document)
        if entry["args"][:1] == ["--catalog-url"]:
            return entry
    raise AssertionError("README has no pinned synthetic-preview stdio block")


def _documented_args():
    args = list(_documented_preview_server()["args"])
    assert args and all(isinstance(arg, str) and arg for arg in args), args
    return args


def _tool_names():
    contract = json.loads(TOOL_CONTRACT.read_text(encoding="utf-8"))
    return [entry["name"] for entry in contract]


# -- the browser route's honesty predicates (unit 10d) ----------------------
#
# The stale claims are matched as PATTERNS, not as "the old sentence", so a
# re-worded denial is caught too, and each pattern is exercised against a
# planted sample in the tests that use it.

#: Claims the shipped source and a fresh build both contradict. Publication
#: STATE is deliberately absent here: whether the deployed page already carries
#: the tools is a fact about the artifact, checked against that artifact below,
#: not a claim the README may never make (unit 11c publication transition).
_BROWSER_DENIAL_PATTERNS = (
    "no browser mcp support",
    "no webmcp registration",
    "no in-page adapter",
    "no javascript implementation",
    "planned, not available",
    "planned and not available",
    "browser integration remains",
    "browser-registration work",
    "works in every browser",
    "supported in every browser",
)

#: A browser claim scoped as "later work" — the D3 regression. The deferral
#: terms name UNFINISHED work; a plain "the browser tools remain a progressive
#: enhancement" is an accurate statement about the shipped design and must not
#: be caught (10d QUALITY M2).
_BROWSER_DEFERRAL = re.compile(
    r"browser[^.]{0,120}(later unit|future work|not implemented|remains? to be"
    r"|not yet (?:implemented|shipped|available))", re.I)


def _browser_denials(text):
    """Every stale or over-broad browser claim in ``text``.

    Broad on purpose: the README *may* say a public origin has no API without
    the flag or trial, but it may not say there is no browser support, and it
    may not call the browser work future work or universal.
    """
    plain = _plain(text).lower()
    found = [pattern for pattern in _BROWSER_DENIAL_PATTERNS if pattern in plain]
    for match in _BROWSER_DEFERRAL.finditer(plain):
        found.append("deferred: " + " ".join(match.group(0).split()))
    return found


# -- publication phase (unit 11c): a fact about the artifact, not a phase to
# -- pin in prose ------------------------------------------------------------
#
# The browser route is implemented in SOURCE and published as a GENERATED page.
# Both a pre-publication checkout (the tracked page is the older two-island
# snapshot) and a fresh-inline checkout (the tracked page carries the inlined
# scripts, i.e. checkpoint C has published a build) are valid states of this
# repository, so the documentation contract is bound to whichever state the
# tracked artifact is actually in instead of hard-wiring one of them.

#: The marker the builder writes around each inlined maintained page script.
PUBLICATION_INLINE_MARKER = 'data-watchdog="webmcp"'

#: Statements that are true only once the page-script build IS the published
#: page. Before that they are a publication claim the artifact contradicts.
_CONSEQUENTIAL_PUBLICATION_CLAIMS = (
    "the deployed dashboard now registers",
    "the deployed dashboard already carries",
    "the deployed page now carries",
    "has been published to the deployed",
    "is already live on the deployed",
)

#: Caveats that are true only WHILE the tracked page predates the page-script
#: build. After publication they are the stale claim instead.
_STALE_ARTIFACT_CAVEATS = (
    "predates the page-script build",
    "still the older two-island snapshot",
    "does not claim it has happened",
)


def _publication_claim_errors(section, tracked_html):
    """Publication prose the artifact contradicts, in either phase.

    ``section`` is the browser section; ``tracked_html`` is the page object
    tracked at ``HEAD``. Before that page carries the inlined scripts, saying
    the deployed dashboard already registers the tools is false; once it
    carries them, keeping a "not published yet" caveat is the stale claim.
    Prose is whitespace-normalized first, because the README is hard-wrapped:
    a claim split across a newline is still that claim.
    """
    lowered = " ".join(_plain(section).lower().split())
    published = PUBLICATION_INLINE_MARKER in tracked_html
    claims = () if published else _CONSEQUENTIAL_PUBLICATION_CLAIMS
    caveats = _STALE_ARTIFACT_CAVEATS if published else ()
    return ([claim for claim in claims if claim in lowered]
            + [caveat for caveat in caveats if caveat in lowered])


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


#: The one documented Pages mirror of the catalogue JSON. It is a mirror only:
#: no code falls back to it, so it is pinned here as a documentation claim.
PAGES_MIRROR_URL = "https://models.dthen.xyz/catalog.json"

#: Any GitHub-raw catalogue URL the README writes, whatever the ref segment.
_RAW_CATALOG_URL = re.compile(
    r"https://raw\.githubusercontent\.com/Dthen/free-inference-watchdog/"
    r"[^/\s`)<>]+/site/catalog\.json")


def _raw_catalog_urls(text):
    """Every GitHub-raw catalogue URL in ``text``, de-duplicated and sorted."""
    return sorted(set(_RAW_CATALOG_URL.findall(text)))


def test_documented_default_stdio_block_is_the_live_published_feed():
    """The PRIMARY client block must select the LIVE default with no source
    flag, and the URL the README documents must be the shipped
    ``DEFAULT_CATALOG_URL`` — bound to ``select_source()``, not restated.

    Task 11: the feed is deployed, so an empty ``args`` array is the working
    vector. A source flag there would pin every reader to a fixture, and a
    second hand-written URL would drift from the constant the server uses.
    """
    from watchdog_catalog import source
    from watchdog_catalog.__main__ import PROGRAM, build_parser, select_source

    entry = _documented_default_server()
    assert PROGRAM == CONSOLE
    assert entry["command"] == CONSOLE, entry["command"]
    assert entry["args"] == [], (
        "the documented live default must pass no source flag, got "
        f"{entry['args']}")

    kind, location = select_source(build_parser().parse_args(entry["args"]))
    assert (kind, location) == ("url", source.DEFAULT_CATALOG_URL), (kind, location)

    section = _section(_readme(), CONSUMER_HEADING)
    assert source.DEFAULT_CATALOG_URL in section, (
        "the consumer section never spells out the live default URL")
    assert _raw_catalog_urls(section) == [source.DEFAULT_CATALOG_URL], (
        "the documented raw catalogue URL drifted from DEFAULT_CATALOG_URL: "
        f"{_raw_catalog_urls(section)}")
    assert PAGES_MIRROR_URL in section, "the Pages mirror URL is undocumented"


def test_documented_synthetic_preview_block_is_pinned_and_labelled():
    """The reproducible preview may stay — but only as an explicitly pinned,
    explicitly synthetic alternative, never as the only working route."""
    from watchdog_catalog.__main__ import build_parser, select_source

    entry = _documented_preview_server()
    args = list(entry["args"])
    assert args == ["--catalog-url", PINNED_FIXTURE_URL], args

    kind, location = select_source(build_parser().parse_args(args))
    assert (kind, location) == ("url", PINNED_FIXTURE_URL), (kind, location)

    section = _section(_readme(), CONSUMER_HEADING)
    assert "synthetic" in section.lower(), "the fixture preview is not labelled"
    assert "not live data" in section.lower(), (
        "the preview is not distinguished from live catalogue data")
    # The preview must be presented as an OPTION next to the default, not as a
    # replacement for it: the default block comes first in the document.
    assert _documented_default_server()["args"] == []


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


#: The obsolete "the feed does not exist yet" wording. The catalogue has been
#: deployed since Task 11, so every one of these clauses is now false. They are
#: checked in README.md AND AGENTS.md, and planted back as mutations below.
_OBSOLETE_FEED_CLAIMS = (
    "not live yet",
    "not yet published",
    "has not been published",
    "not published yet",
    "absent from master",
    "stays dead",
    "rollout is pending",
    "still gated on rollout",
)


def _obsolete_feed_claims(text):
    """The obsolete un-deployed-feed clauses ``text`` still asserts."""
    lowered = _plain(text).lower()
    return [claim for claim in _OBSOLETE_FEED_CLAIMS if claim in lowered]


def test_consumer_section_documents_the_live_feed_as_the_default():
    """The feed is published, so the consumer route must say so: the raw JSON is
    the default source, the Pages copy is a MIRROR (never an implicit failover),
    the dashboard is not the feed, and reachability is not promised forever."""
    from watchdog_catalog import source

    text = _readme()
    section = _section(text, CONSUMER_HEADING)
    lowered = _plain(section).lower()

    assert _obsolete_feed_claims(text) == [], (
        "the README still claims the feed is unpublished: "
        f"{_obsolete_feed_claims(text)}")

    assert "### Public JSON feed" in section, "public JSON feed section is gone"
    assert source.DEFAULT_CATALOG_URL in section, "the live feed URL is missing"
    assert PAGES_MIRROR_URL in section, "the Pages mirror URL is missing"
    assert "mirror" in lowered, "the Pages copy is not described as a mirror"
    assert "failover" in lowered, (
        "the mirror must be distinguished from an implicit failover")

    for phrase in ("schema_version", "snapshot_id", "tick_epoch",
                   "provider_order", "providers"):
        assert f"`{phrase}`" in section, f"feed schema omits {phrase}"

    # "Live" is not a promise of perpetual reachability: the true failure
    # contract stays documented, and the dashboard stays distinct from the feed.
    assert "source_unavailable" in lowered, \
        "the default-route failure mode must be documented"
    assert "cache_fallback" in lowered, \
        "the validated-last-snapshot fallback must stay documented"
    dashboard = _paragraph_with(section, "dashboard")
    assert "dashboard" in dashboard.lower() and "feed" in dashboard.lower(), \
        "the dashboard must be distinguished from the JSON feed"

    # Sensitivity: the removed draft wording is caught by the same predicate.
    planted = f"{text}\nThe default feed URL is **not live yet**.\n"
    assert _obsolete_feed_claims(planted) == ["not live yet"], (
        "the obsolete-feed-claim check is not sensitive to a planted claim")


def test_consumer_section_keeps_webmcp_distinct_from_a_remote_url():
    """The consumer route must not deny the shipped browser support, and must
    still distinguish an in-page browser API from a remotely connectable MCP
    HTTP URL. The stale "planned, not available" wording is planted below so
    the predicate is proven sensitive rather than vacuous (unit 10d)."""
    section = _section(_readme(), CONSUMER_HEADING)
    lowered = _plain(section).lower()

    assert _browser_denials(_readme()) == [], (
        "README still denies the browser support this ref ships")
    assert "webmcp" in lowered, "the consumer route no longer mentions WebMCP"
    assert "in-page" in lowered, \
        "the consumer route must say WebMCP is an in-page API"
    assert "not a remotely connectable mcp http url" in lowered, \
        "WebMCP must be distinguished from a remote MCP HTTP URL"
    assert "browser-webmcp-tools-in-the-dashboard" in section, \
        "the consumer route must route browser readers to the browser section"

    planted = section + (
        "\nBrowser support is planned, not available: there is no in-page "
        "adapter and no JavaScript implementation here.\n")
    assert _browser_denials(planted), \
        "the browser-denial check is not sensitive to a planted denial"


# -- browser (WebMCP) route, EXECUTED (unit 10d) ----------------------------
#
# The browser claim is a claim about a GENERATED ARTIFACT, so these tests build
# the real page from a synthetic roster through the real builder, read the two
# inert islands and the three inlined classic scripts out of the generated
# HTML, and RUN those scripts in a Node realm with a fake `document`. What the
# README says about setup — the native API it feature-detects, the five
# registered tools, the annotation hints, the browser provenance and the
# "never truncate to fit the recommendation" rule — is bound to what the page
# actually does, and the payload is compared against the shared Python core
# over the SAME embedded snapshot. Availability is checked as a SCOPE (flag,
# trial, secure context, permissions policy) because "conditional" is the only
# accurate statement, and the stale denials are planted so the predicate is
# proven sensitive.

BROWSER_HEADING = "### Browser (WebMCP) tools in the dashboard"
BUILDER = REPO / "build_site.py"
BROWSER_PAGE_SCRIPT_MARKERS = ("catalog-query", "webmcp", "boot")
#: Chrome's security guidance RECOMMENDATION — not a protocol limit, and not
#: this project's cap (browser contract 4.6).
RECOMMENDED_OUTPUT_CHARS = 1500

#: Synthetic roster: three gateways (one tracking zero ids), a ~6K-character
#: upstream description, and ids that sort differently from insertion order.
BROWSER_ROSTER_PROVIDERS = {
    "nous": ["vendor-a/browser-huge:free", "vendor-a/browser-small",
             "vendor-b/shared-model"],
    "tokenrouter": ["vendor-b/shared-model", "vendor-b/browser-tool"],
    "amd": [],
}
LONG_UPSTREAM_DESCRIPTION = "Synthetic long upstream description. " * 150


def _browser_model(name, description=None):
    return {
        "name": name,
        "description": description if description is not None
        else f"Synthetic description for {name}.",
        "context_length": 8192,
        "architecture": {"input_modalities": ["text"],
                         "output_modalities": ["text"]},
        "supported_parameters": ["temperature"],
        "pricing": {"prompt": 0.5, "completion": 1.5},
    }


def _browser_provider_models():
    models = {}
    for provider, ids in BROWSER_ROSTER_PROVIDERS.items():
        models[provider] = {
            raw: _browser_model(
                f"Browser {raw.split('/')[-1]}",
                LONG_UPSTREAM_DESCRIPTION if "huge" in raw else None)
            for raw in ids}
    return models


def _page_node_binary():
    """Node for the generated-page execution checks (same rules as parity)."""
    import shutil

    override = os.environ.get("CONTRACT_NODE")
    if override is not None:
        if not override:
            pytest.fail("CONTRACT_NODE is set but empty; set it to a Node "
                        "binary or unset it to use PATH discovery")
        resolved = shutil.which(override)
        if resolved is None and os.path.isfile(override):
            resolved = os.path.abspath(override)
        if resolved is None:
            pytest.fail(f"CONTRACT_NODE={override!r} is not an executable Node "
                        "binary; the explicit override is authoritative")
        return resolved
    resolved = shutil.which("node")
    if resolved is None:
        pytest.skip("Node is not on PATH: install Node or set CONTRACT_NODE to "
                    "run the generated-page execution checks")
        raise AssertionError("unreachable: pytest.skip always raises")
    return resolved


def _build_browser_page(root):
    """Build a real page from the synthetic roster; return its HTML."""
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (state / "roster.json").write_text(json.dumps({
        "tick_epoch": 1787800000,
        "providers": BROWSER_ROSTER_PROVIDERS,
        "provider_models": _browser_provider_models(),
        "stale_providers": [],
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--root", str(root)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return (root / "site" / "index.html").read_text(encoding="utf-8")


def _browser_page_parts(html):
    """The page's two inert islands and its three classic scripts, in order."""
    def _island(island_id):
        match = re.search(
            r'<script type="application/json" id="'
            + re.escape(island_id) + r'">(.*?)</script>', html, re.S)
        assert match, f"the generated page has no {island_id} island"
        return match.group(1)

    scripts = dict(re.findall(
        r'<script data-watchdog="([a-z-]+)">(.*?)</script>', html, re.S))
    return {
        "islands": {"catalog-data": _island("catalog-data"),
                    "tool-contract": _island("tool-contract")},
        "scripts": scripts,
    }


#: Executes the page's own classic scripts against a fake in-page WebMCP host —
#: the documented setup path, with no browser and no network.
_PAGE_EXECUTION_JS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const spec = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const registrations = [];
const documentStub = {
  getElementById(id) {
    const text = spec.islands[id];
    return text === undefined ? null : { textContent: text };
  },
  modelContext: {
    registerTool(descriptor) {
      registrations.push(descriptor);
      return Promise.resolve();
    },
    getTools() {
      return Promise.resolve(registrations.map((d) => ({ name: d.name })));
    }
  }
};
const sandbox = { document: documentStub };
vm.createContext(sandbox);
for (const body of spec.scripts) { vm.runInContext(body, sandbox); }
(async () => {
  const setup = await vm.runInContext("WatchdogWebMCP.setup()", sandbox);
  const results = {};
  for (const call of spec.calls) {
    const descriptor = registrations.find((d) => d.name === call.tool);
    if (!descriptor) { throw new Error("no registered tool: " + call.tool); }
    const text = await descriptor.execute(call.args);
    results[call.label] = { type: typeof text, text: text };
  }
  console.log(JSON.stringify({
    setup: { status: setup.status, reason: setup.reason,
             registered: setup.registered },
    registered: registrations.map((d) => d.name),
    annotations: registrations.map((d) => d.annotations || null),
    results: results
  }));
})().catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1); });
"""

BROWSER_CALLS = (
    {"label": "paged", "tool": "search_models",
     "args": {"query": "browser", "limit": 2}},
    {"label": "bulk", "tool": "list_free_models", "args": {}},
    {"label": "bad_provider", "tool": "search_models",
     "args": {"provider": "not-a-gateway"}},
)


@pytest.fixture(scope="module")
def browser_page(tmp_path_factory):
    """One real build, then one real execution of the page's own scripts."""
    node = _page_node_binary()
    root = tmp_path_factory.mktemp("browser-page")
    html = _build_browser_page(root)
    parts = _browser_page_parts(html)
    assert sorted(parts["scripts"]) == sorted(BROWSER_PAGE_SCRIPT_MARKERS), (
        "the generated page does not carry the three maintained page scripts")

    spec = {
        "islands": parts["islands"],
        "scripts": [parts["scripts"][marker]
                    for marker in BROWSER_PAGE_SCRIPT_MARKERS],
        "calls": list(BROWSER_CALLS),
    }
    spec_path = root / "page-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    runner = root / "run-page.cjs"
    runner.write_text(_PAGE_EXECUTION_JS, encoding="utf-8")
    proc = subprocess.run([node, str(runner), str(spec_path)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    execution = json.loads(proc.stdout)
    assert "error" not in execution, execution["error"]

    from watchdog_catalog import catalog, query

    snapshot = catalog.load_snapshot(parts["islands"]["catalog-data"])
    return {
        "html": html,
        "parts": parts,
        "execution": execution,
        "snapshot": snapshot,
        "bulk_expected": query.list_free_models(snapshot, {}),
        "paged_expected": query.search_models(
            snapshot, {"query": "browser", "limit": 2}),
        "total_ids": len({raw for ids in BROWSER_ROSTER_PROVIDERS.values()
                          for raw in ids}),
    }


def _json_result(browser_page, label):
    """One native-path result, decoded with the SHARED strict loader."""
    from watchdog_catalog import catalog

    entry = browser_page["execution"]["results"][label]
    assert entry["type"] == "string", (
        f"{label} must return JSON TEXT, not a bare object: {entry['type']}")
    return catalog.load_json(entry["text"])


def test_documented_browser_setup_registers_the_page_tools(browser_page):
    """The README's setup claim is executed, not read: the built page's own
    scripts register exactly the five shared tools, from the embedded contract,
    with the three documented annotations."""
    execution = browser_page["execution"]
    assert execution["setup"]["status"] == "registered", execution["setup"]
    assert execution["registered"] == _tool_names(), (
        "the page did not register exactly the shared tool set")
    assert execution["setup"]["registered"] == _tool_names()

    for annotations in execution["annotations"]:
        assert annotations["readOnlyHint"] is True
        assert annotations["untrustedContentHint"] is True
        assert annotations["consequentialHint"] is False

    section = _plain(_section(_readme(), BROWSER_HEADING))
    for name in _tool_names():
        assert name in section, f"the browser section never names {name}"
    assert "document.modelContext.registerTool" in section, \
        "the documented detection point is not the shipped one"
    for hint in ("readOnlyHint", "untrustedContentHint", "consequentialHint"):
        assert hint in section, f"the browser section omits {hint}"

    # The hints the README states are the ones the shipped adapter carries.
    adapter = (REPO / "web" / "webmcp.js").read_text(encoding="utf-8")
    for hint in ("readOnlyHint", "untrustedContentHint", "consequentialHint"):
        assert hint in adapter, f"the shipped adapter no longer sets {hint}"


def test_documented_browser_payloads_match_the_embedded_snapshot(browser_page):
    """Browser results are `source: {mode: browser}` wrapped PURE payloads over
    the page's own snapshot: complete bulk output, a real page with a truthful
    `next_offset`, and no browser-only truncation or dropped metadata."""
    paged = _json_result(browser_page, "paged")
    assert paged["source"] == {"mode": "browser"}
    expected = browser_page["paged_expected"]
    paged_core = {key: value for key, value in paged.items() if key != "source"}
    assert paged_core == expected, "the page answered differently from the core"

    bulk = _json_result(browser_page, "bulk")
    assert bulk["source"] == {"mode": "browser"}
    bulk_core = {key: value for key, value in bulk.items() if key != "source"}
    assert bulk_core == browser_page["bulk_expected"], (
        "bulk output is not the complete core payload")
    assert bulk["total_ids"] == browser_page["total_ids"]
    assert sorted(bulk["providers"]) == sorted(
        browser_page["snapshot"]["provider_order"]), (
        "every snapshot gateway must survive the browser boundary")
    assert any(not ids for ids in bulk["providers"].values()), (
        "the fixture no longer carries a zero-id gateway")

    bad = _json_result(browser_page, "bad_provider")
    assert bad["ok"] is False and bad["error"]["code"] == "unknown_provider"
    assert bad["source"] == {"mode": "browser"}, (
        "argument errors must carry the same honest provenance")

    section = _plain(_section(_readme(), BROWSER_HEADING))
    assert '"mode": "browser"' in section, \
        "the documented browser provenance is not the shipped one"
    assert "no browser-only truncation" in section, \
        "the section dropped the no-truncation promise"


def test_bulk_output_is_documented_as_a_recommendation_not_a_cap(browser_page):
    """Chrome RECOMMENDS ~1.5K characters per output; the page must not truncate
    to fit it, and the README must say so and route routine lookups to
    `search_models`."""
    paged_text = browser_page["execution"]["results"]["paged"]["text"]
    bulk_text = browser_page["execution"]["results"]["bulk"]["text"]
    assert len(paged_text) > RECOMMENDED_OUTPUT_CHARS, (
        "the fixture no longer exercises the oversize case")
    assert len(bulk_text) > RECOMMENDED_OUTPUT_CHARS

    section = _plain(_section(_readme(), BROWSER_HEADING))
    lowered = section.lower()
    assert "recommendation, not a cap" in lowered
    assert "1.5k" in lowered
    assert "no browser-only truncation" in lowered
    assert "search_models" in section and "limit" in section, \
        "the section no longer routes routine lookups to a bounded search"
    assert "get_model" in section, \
        "the section no longer routes detail lookups to get_model"

    # Same guidance in the shipped descriptions the page registers.
    contract = {entry["name"]: entry["description"]
                for entry in json.loads(TOOL_CONTRACT.read_text(encoding="utf-8"))}
    for bulk_tool in ("list_free_models", "list_endpoints"):
        description = contract[bulk_tool]
        assert "search_models" in description
        assert "1-5" in description
        assert "no hard universal output cap" in description


def test_browser_availability_is_scoped_not_universal():
    """Ordinary-visitor availability is stated accurately: the development flag,
    the Chrome 149+ origin trial (optional, separately authorized), the secure
    context, and the `tools` permissions policy — with no universal claim."""
    section = _plain(_section(_readme(), BROWSER_HEADING))
    lowered = section.lower()

    assert "no universal browser support" in lowered
    assert "chrome://flags/#enable-webmcp-testing" in section
    assert "origin trial" in lowered and "chrome 149" in lowered
    assert "secure context" in lowered
    assert "https://" in section and "file://" in section
    assert "permissions policy" in lowered and 'allow="tools"' in section
    assert "optional" in lowered and "separately authorized" in lowered, \
        "origin-trial enrolment must stay optional and separately authorized"
    assert "never" in lowered and "enrol" in lowered

    assert _browser_denials(_readme()) == [], (
        "README carries a stale or over-broad browser claim: "
        f"{_browser_denials(_readme())}")


def _tracked_page_html():
    """The page object committed at HEAD, or a skip outside a Git checkout."""
    tracked = subprocess.run(
        ["git", "show", "HEAD:site/index.html"],
        cwd=str(REPO), capture_output=True, text=True)
    if tracked.returncode != 0:
        pytest.skip("not a git checkout with a tracked site/index.html")
    return tracked.stdout


def test_browser_publication_status_is_bound_to_the_tracked_artifact(browser_page):
    """The publication paragraph must stay true of the artifact in THIS checkout.

    Before checkpoint C publishes a build, the tracked page is the older
    two-island snapshot, so the docs may not claim the deployed dashboard
    already registers the tools. Once a refreshed page is committed, that same
    caveat becomes the false statement instead. Required in both phases: the
    publication boundary is spelled out, the artifact check that decides it is
    documented, and a local build is never called publication.
    """
    section = _plain(_section(_readme(), BROWSER_HEADING))
    lowered = " ".join(section.lower().split())
    assert "publication status" in lowered
    assert "site/index.html" in section
    assert "separately authorized" in lowered
    assert "a local build is never publication" in lowered
    assert "data-watchdog" in section, (
        "the section does not say how a reader can tell a published build "
        "from an older page")

    # A fresh build of this source really does carry the inlined adapter.
    assert PUBLICATION_INLINE_MARKER in browser_page["html"], \
        "a fresh build no longer carries the inlined adapter"

    tracked = _tracked_page_html()
    phase = ("fresh-inline" if PUBLICATION_INLINE_MARKER in tracked
             else "pre-publication")
    assert _publication_claim_errors(section, tracked) == [], (
        "the publication paragraph contradicts the tracked site/index.html "
        f"({phase} phase): {_publication_claim_errors(section, tracked)}")

    # The same paragraph must stay accurate once a page BUILT from this source
    # is the published one, so the transition never requires a docs rewrite.
    # `browser_page["html"]` is the real builder output for this checkout.
    assert _publication_claim_errors(section, browser_page["html"]) == [], (
        "the publication paragraph would become stale once a page built from "
        "this ref is published")


@pytest.mark.parametrize("published, prose, expected", [
    (False,
     "The tracked site/index.html shows the last published page; a local build "
     "is never publication. Every build carries the data-watchdog script "
     "islands.",
     []),
    (False,
     "The deployed dashboard now registers the five tools.",
     ["the deployed dashboard now registers"]),
    (True,
     "The tracked site/index.html shows the last published page; a local build "
     "is never publication. Every build carries the data-watchdog script "
     "islands.",
     []),
    (True,
     "The tracked site/index.html is still the older two-island snapshot and "
     "does not claim it has happened.",
     ["still the older two-island snapshot", "does not claim it has happened"]),
    # Hard-wrapped prose is the ordinary README case, so a claim split across a
    # newline must be read as the same sentence it is on one line.
    (False,
     "The deployed dashboard now\nregisters the five tools.",
     ["the deployed dashboard now registers"]),
    (True,
     "The tracked site/index.html is still the older\ntwo-island snapshot and "
     "does\nnot claim it has happened.",
     ["still the older two-island snapshot", "does not claim it has happened"]),
])
def test_publication_claim_predicate_accepts_both_valid_phases(
        published, prose, expected):
    """Both artifact phases are supported and each direction stays sensitive.

    A phase-neutral paragraph passes in both. A deployment claim fails while
    the tracked artifact is old; the pre-publication caveat fails once that
    artifact carries the inlined scripts. This is the planted control for the
    transition, so neither branch is merely assumed.
    """
    tracked = ("<html/>" if not published
               else f'<script {PUBLICATION_INLINE_MARKER}></script>')
    assert _publication_claim_errors(prose, tracked) == expected


def test_agents_documents_the_browser_surface_it_ships():
    """AGENTS.md is the architecture map: it must describe the shipped browser
    surface and its availability limits, not the retired 'planned' rule."""
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    plain = _plain(agents)
    lowered = plain.lower()

    assert _browser_denials(agents) == [], (
        f"AGENTS.md still carries a stale browser claim: {_browser_denials(agents)}")
    assert "## browser surface (webmcp)" in lowered
    for path in ("web/catalog-query.js", "web/webmcp.js", "build_site.py"):
        assert path in agents, f"AGENTS.md no longer names {path}"
    assert "document.modelContext.registerTool" in agents
    assert "chrome://flags/#enable-webmcp-testing" in agents
    assert "origin trial" in lowered
    assert "remotely connectable mcp http url" in lowered
    assert "no polyfill" in lowered
    assert "source: {mode: \"browser\"}" in agents.replace("**", "")

    # The site allowlist must keep exactly two generated payloads and say the
    # inlined sources do not widen it.
    assert "exactly two generated site payloads" in agents
    assert "do not widen the allowlist" in agents
    assert "a local build is never publication" in agents

    planted = agents.replace(
        "## Browser surface (WebMCP)",
        "## Browser surface (WebMCP)\n- Browser-native WebMCP is PLANNED, not "
        "implemented: no in-page adapter.")
    assert _browser_denials(planted), \
        "the AGENTS browser-claim check is not sensitive to a planted denial"


def test_shared_tool_definitions_are_documented_as_shipped():
    """The shared contract's closing paragraph must not call packaging,
    embedding or browser registration 'later units' (D3)."""
    text = _readme()
    heading = "### Shared tool definitions (frozen resource)"
    section = _section(text, heading)
    plain = _plain(section)
    assert _browser_denials(section) == [], (
        f"the shared-definitions section still defers browser work: "
        f"{_browser_denials(section)}")
    assert "web/webmcp.js" in section
    assert "tool-contract" in section
    assert "not\nlater work" in section or "not later work" in " ".join(section.split()), \
        "the section must say the shipped work is shipped"


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


# ---------- AGENTS.md architecture guidance ----------
#
# AGENTS.md is the agent-facing architecture note for the code that already
# ships. Its one executable claim is a POINTER claim: every module or script it
# names must be a real file in this checkout. That is the exact failure mode the
# standalone catalogue work produced (stale module comments and proposed
# filenames surviving a rename), and it is checked as a resource, not as prose.

AGENTS = REPO / "AGENTS.md"

#: Code paths AGENTS names in backticks. Deliberately limited to `.py`/`.sh`:
#: generated and runtime-only data paths (`site/catalog.json`, `state/...`) are
#: covered by the builder/publication tests, not by existence in a clean tree.
_NAMED_CODE_PATH = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|sh))`")


def _missing_named_code_paths(text):
    """The code paths ``text`` names that do not exist under ``REPO``."""
    return sorted({name for name in _NAMED_CODE_PATH.findall(text)
                   if not (REPO / name).is_file()})


def test_agents_names_only_code_paths_that_exist():
    """AGENTS.md may only name modules and scripts that really exist.

    The architecture map is guidance an agent acts on, so a name that survived
    a rename sends the next reader at a file that is not there. The sensitivity
    proof below plants exactly such a name and requires the same predicate to
    catch it, so this is not a test that would pass on unreadable input.
    """
    text = AGENTS.read_text(encoding="utf-8")
    named = {name for name in _NAMED_CODE_PATH.findall(text)}

    assert named, "AGENTS.md names no modules at all — the map was dropped"
    assert _missing_named_code_paths(text) == [], (
        "AGENTS.md names code paths that do not exist: "
        f"{_missing_named_code_paths(text)}"
    )

    # The two publication-gate scripts and the standalone package root are the
    # names a reader is most likely to act on, so require them explicitly. The
    # retired root local-state server is deliberately NOT required: naming it
    # would fail the stale-name check above, because the file is gone.
    for required in ("scripts/publish_site.sh", "scripts/validate_site.py",
                     "watchdog_catalog/__init__.py", "watchdog_catalog/query.py",
                     "watchdog_catalog/source.py", "watchdog_catalog/server.py",
                     "watchdog_catalog/__main__.py", "catalog_export.py",
                     "build_site.py"):
        assert required in named, f"AGENTS.md no longer names {required}"

    planted = "watchdog_catalog/proposed_stale_module.py"
    mutated = f"{text}\n- `{planted}`\n"
    assert _missing_named_code_paths(mutated) == [planted], (
        "the stale-name check is not sensitive to a planted absent module"
    )


# ---------- AGENTS.md clock and tool-schema claims ----------
#
# The architecture note also makes CLAIMS about behaviour that already exists:
# when observation time is taken, who may read a clock, and what the packaged
# tool resource actually contains. Parent verification found the first draft
# wrong in exactly those places, so each corrected claim is bound to the shipped
# object it describes, and the obsolete sentence is reproduced below as a
# mutation the same predicate must reject.

_AGENTS_STALE_CLOCK_CLAIMS = (
    "Observation time is the injected `now`",
    "Only the transport's `wall_clock` seam and the producer's tick",
)

_AGENTS_STALE_RESOURCE_CLAIM = "never types, bounds, defaults or an `inputSchema`"


def _agents_stale_clock_claims(text):
    """The obsolete clock sentences ``text`` still asserts."""
    return [claim for claim in _AGENTS_STALE_CLOCK_CLAIMS if claim in text]


def _agents_denies_resource_schema(text):
    """Whether ``text`` denies that the tool resource ships an ``inputSchema``."""
    return _AGENTS_STALE_RESOURCE_CLAIM in text


def test_agents_binds_observation_time_to_the_snapshot_tick():
    """Observation time is the snapshot's `tick_epoch`; the injected `now` is the
    query's evaluation time, and the async source owns wall/monotonic seams read
    at operation time only.

    The bindings are structural and behavioural rather than prose: `now` must
    stay an injected keyword-only epoch (never a public tool argument), the
    source must still expose both seams, and a clock that raises on any read
    must survive construction -- which is what "never at construction" means in
    code.
    """
    import inspect

    from watchdog_catalog import query, source

    text = AGENTS.read_text(encoding="utf-8")
    stale = _agents_stale_clock_claims(text)
    assert stale == [], f"AGENTS.md still asserts an obsolete clock claim: {stale}"

    # The corrected wording must name the real observation stamp the payload
    # carries and the async chapter that schedules retrieval.
    assert "tick_epoch" in text, "AGENTS.md lost the snapshot observation stamp"
    assert "AsyncCatalogSource" in text, "AGENTS.md lost the async source owner"

    now = inspect.signature(query.watchdog_status).parameters["now"]
    assert now.kind is inspect.Parameter.KEYWORD_ONLY, \
        "`now` must stay injected, not a positional or public argument"
    assert now.default is inspect.Parameter.empty, \
        "`now` must stay defaultless so no query silently invents a clock"

    init = inspect.signature(source.AsyncCatalogSource.__init__).parameters
    for seam in ("wall_clock", "monotonic_clock"):
        assert seam in init, f"AsyncCatalogSource lost its {seam} seam"

    def exploding_clock():
        raise AssertionError("a clock was read while constructing the source")

    source.AsyncCatalogSource("file", str(FIXTURE_DIR / "lookup.json"),
                              wall_clock=exploding_clock,
                              monotonic_clock=exploding_clock)

    # Sensitivity: the same predicate must catch the sentence the parent review
    # found, so this is a wrong-claim check and not a vacuous pass.
    planted = f"{text}\n- {_AGENTS_STALE_CLOCK_CLAIMS[0]}.\n"
    assert _agents_stale_clock_claims(planted) == [_AGENTS_STALE_CLOCK_CLAIMS[0]], (
        "the stale-clock-claim check is not sensitive to a planted wrong claim"
    )


def test_agents_describes_the_tool_schema_contract_it_ships():
    """`tools.json` ships a full `inputSchema`; only the transport integration
    must not install it as the generated SDK schema.

    The resource is read as the contract it is -- every tool carries an object
    schema with properties, a required list and closed additionalProperties --
    and AGENTS.md must describe that rather than deny it. The obsolete
    "descriptions only, never ... an `inputSchema`" sentence is reproduced as a
    mutation the same predicate rejects.
    """
    text = AGENTS.read_text(encoding="utf-8")
    tools = json.loads(TOOL_CONTRACT.read_text(encoding="utf-8"))

    assert tools, "the packaged tool contract is empty"
    for tool in tools:
        schema = tool.get("inputSchema")
        assert isinstance(schema, dict), f"{tool.get('name')} ships no inputSchema"
        assert schema.get("type") == "object", \
            f"{tool['name']} inputSchema is not an object schema"
        assert isinstance(schema.get("properties"), dict), \
            f"{tool['name']} inputSchema has no properties"
        assert isinstance(schema.get("required"), list), \
            f"{tool['name']} inputSchema has no required list"
        assert schema.get("additionalProperties") is False, \
            f"{tool['name']} inputSchema is not closed to unknown arguments"

    assert not _agents_denies_resource_schema(text), (
        "AGENTS.md still denies the schema this resource ships: "
        f"{_AGENTS_STALE_RESOURCE_CLAIM!r}"
    )
    assert "inputSchema" in text, "AGENTS.md no longer describes the schema contract"
    assert "watchdog_catalog/server.py" in text, \
        "AGENTS.md lost the transport that scopes the rule"

    planted = f"{text}\n- supplies descriptions only, {_AGENTS_STALE_RESOURCE_CLAIM}.\n"
    assert _agents_denies_resource_schema(planted), (
        "the stale-resource-claim check is not sensitive to a planted wrong claim"
    )


# ---------- AGENTS.md deployed-feed claims (unit 11d1) ----------
#
# AGENTS.md carried the same pre-deployment status as the README: the feed was
# described as absent from master, so the default URL "stays dead". Task 11
# deployed it. The corrected note is bound to the real default constant and the
# real Pages mirror URL, and both obsolete sentences are reproduced as
# mutations the same predicate must reject.


def test_agents_documents_the_deployed_default_feed_and_its_mirror():
    """AGENTS.md must describe the deployed feed: the raw GitHub copy as the
    default source, the Pages copy as a mirror with no implicit fallback, and
    no new production authority as a result of deployment."""
    from watchdog_catalog import source

    text = AGENTS.read_text(encoding="utf-8")
    lowered = _plain(text).lower()

    assert _obsolete_feed_claims(text) == [], (
        f"AGENTS.md still claims the feed is unpublished: {_obsolete_feed_claims(text)}")
    assert source.DEFAULT_CATALOG_URL in text, \
        "AGENTS.md lost the shipped default feed URL"
    assert PAGES_MIRROR_URL in text, "AGENTS.md lost the Pages mirror URL"
    assert "mirror" in lowered and "fallback" in lowered, \
        "AGENTS.md must call the Pages copy a mirror, not a fallback"
    assert "never run the publisher" in lowered, \
        "AGENTS.md lost the rule against running the publisher to check docs"

    # Sensitivity: each removed pre-deployment sentence is caught when planted.
    for claim in ("site/catalog.json is not published yet",
                  "so the default feed URL stays dead"):
        planted = f"{text}\n- {claim}.\n"
        assert _obsolete_feed_claims(planted), (
            f"the obsolete-feed-claim check misses {claim!r}")
