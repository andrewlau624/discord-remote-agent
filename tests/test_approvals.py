"""The auto-approve panel: what it offers, and what it remembers."""

from __future__ import annotations

from src.approvals import (
    _DIGITS,
    _page,
    ApprovalPrefs,
    KNOWN_TOOLS,
    panel_embed,
)


def test_digits_are_keycap_emoji():
    assert _DIGITS[0] == "1️⃣"
    assert len(_DIGITS) == 9


def test_allows_only_approved_tools():
    p = ApprovalPrefs(approved={"Read"})
    assert p.allows("Read")
    assert not p.allows("Edit")


def test_accept_all_overrides_the_per_tool_list():
    p = ApprovalPrefs(accept_all=True)
    assert p.allows("Edit") and p.allows("anything-at-all")


def test_note_seen_reports_only_the_first_sighting():
    p = ApprovalPrefs()
    assert p.note_seen("mcp__ctx__query") is True
    assert p.note_seen("mcp__ctx__query") is False
    assert p.note_seen("") is False


def test_the_panel_offers_builtins_plus_whatever_turns_up():
    p = ApprovalPrefs()
    p.note_seen("mcp__context7__query-docs")
    tools = p.tools()
    assert set(KNOWN_TOOLS) <= set(tools)
    assert "mcp__context7__query-docs" in tools
    assert len(tools) == len(set(tools)), "no duplicates"


def test_approved_tools_sort_first():
    p = ApprovalPrefs(approved={"Write"})
    assert p.tools()[0] == "Write"


def test_pages_cover_every_tool_exactly_once():
    p = ApprovalPrefs(seen={f"Tool{i:02d}" for i in range(25)})
    _, total = _page(p, 0)
    seen: list[str] = []
    for i in range(total):
        rows, _ = _page(p, i)
        assert len(rows) <= len(_DIGITS)
        seen += rows
    assert sorted(seen) == p.tools()


def test_paging_wraps():
    p = ApprovalPrefs(seen={f"Tool{i:02d}" for i in range(25)})
    _, total = _page(p, 0)
    assert _page(p, total)[0] == _page(p, 0)[0]


def test_a_toggle_follows_the_tool_not_the_row():
    # New tools shift rows around; a setting must not jump to its neighbour.
    p = ApprovalPrefs()
    target = _page(p, 0)[0][3]
    p.approved.add(target)
    p.note_seen("AAA_SortsFirst")
    assert p.allows(target)


def test_embed_renders_in_both_states():
    p = ApprovalPrefs(approved={"Read"})
    assert panel_embed(p, 0).title == "Auto-approve"
    p.accept_all = True
    warned = panel_embed(p, 0)
    assert "Accept all is ON" in warned.description
    assert warned.color.value == 0xE67E22, "accept-all must look different"


def test_config_seeds_the_first_run_only(tmp_path):
    path = str(tmp_path / "approvals.json")
    first = ApprovalPrefs.load(path, seed=["Read", "Bash"])
    assert first.approved == {"Read", "Bash"}

    first.approved.add("Edit")
    first.accept_all = True
    first.save(path)

    second = ApprovalPrefs.load(path, seed=["ShouldBeIgnored"])
    assert second.approved == {"Read", "Bash", "Edit"}
    assert "ShouldBeIgnored" not in second.approved
    assert second.accept_all is True


def test_load_tolerates_a_corrupt_file(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text("{ not json")
    assert ApprovalPrefs.load(str(path), seed=["Read"]).approved == {"Read"}
