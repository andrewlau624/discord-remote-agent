"""Provider normalization: modes, forum names, and the new-arg parser."""

from __future__ import annotations

from src.bot import _parse_new_args, canonical_mode
from src.forum import forum_name
from src.providers import PROVIDERS, modes_for_provider


def test_every_provider_declares_modes_and_a_default():
    for name in PROVIDERS:
        modes = modes_for_provider(name)
        assert "default" in modes, name


def test_forum_names_are_provider_scoped():
    assert forum_name("claude") == "sessions-claude"
    assert forum_name("opencode") == "sessions-opencode"


def test_parse_new_args_full_form():
    repo, branch, mode, provider, err = _parse_new_args(
        "repo:proj branch:feat/x mode:plan provider:opencode"
    )
    assert err is None
    assert (repo, branch, mode, provider) == ("proj", "feat/x", "plan", "opencode")


def test_parse_new_args_bare_repo_and_alias_mode():
    repo, branch, mode, provider, err = _parse_new_args("myrepo bypass")
    assert err is None
    assert repo == "myrepo" and mode == "bypassPermissions"


def test_parse_new_args_rejects_unknown_provider():
    _, _, _, _, err = _parse_new_args("provider:nope")
    assert err and "nope" in err


def test_canonical_mode_aliases():
    assert canonical_mode("bypass") == "bypassPermissions"
    assert canonical_mode("edit") == "acceptEdits"
