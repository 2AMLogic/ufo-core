"""The website-building profile's tool set, read against the research tools it may name. The
profile is the open `sites` extension's; `search_web`, `search_vertical` and `fetch_url` are the
hosted research extension's, so the one test that reads both tables is proven beside research."""

from ufo_ext_repl.manifest import JS_REPL_TOOL, XLSX_REPL_TOOL
from ufo_ext_research.tools import FETCH_URL_TOOL, SEARCH_VERTICAL_TOOL, SEARCH_WEB_TOOL
from ufo_ext_sites.subagent import WEBSITE_BUILDING_PROFILE
from ufo_ext_sites.tools import SITES_TOOLS

from ufo.host.tools.builtins import BUILTIN_TOOLS


def test_the_website_building_profile_names_only_meaningful_tools() -> None:
    """It builds, serves, validates and hosts — and delivers nothing itself."""
    names = set(WEBSITE_BUILDING_PROFILE.tool_names)
    available = (
        {tool.canonical_id for tool in SITES_TOOLS}
        | {tool.name for tool in BUILTIN_TOOLS}
        | {JS_REPL_TOOL, XLSX_REPL_TOOL}
        | {SEARCH_WEB_TOOL, SEARCH_VERTICAL_TOOL, FETCH_URL_TOOL}
    )
    assert names <= available
    assert {JS_REPL_TOOL, XLSX_REPL_TOOL} <= names
    assert {
        "start_server",
        "write",
        "js_repl",
        "action:site:deploy_website",
        "action:agent:set_homepage",
    } <= names
    assert {tool.name for tool in SITES_TOOLS if tool.profile_only}.isdisjoint(names)
    assert "share_file" not in names
    assert WEBSITE_BUILDING_PROFILE.input_model.model_validate(
        {"objective": "build a landing page"}
    ).objective
    assert WEBSITE_BUILDING_PROFILE.max_rounds == 100
