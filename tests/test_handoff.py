"""Composing what a fresh session is told about the one it replaces."""

from __future__ import annotations

import subprocess

from src.handoff import compose_seed, recent_exchange, working_state

HISTORY = [
    ("user", "add the parser"),
    ("assistant", "added it in parse.py"),
    ("user", "   "),
    ("assistant", "tests pass"),
]


def test_blank_messages_are_dropped():
    out = recent_exchange(HISTORY)
    assert out.count("**User:**") == 1
    assert out.count("**Assistant:**") == 2


def test_only_the_tail_is_carried():
    long = [("user", f"msg {i}") for i in range(50)]
    out = recent_exchange(long, limit=3)
    assert "msg 49" in out and "msg 47" in out
    assert "msg 46" not in out


def test_long_messages_are_truncated():
    out = recent_exchange([("user", "x" * 5000)])
    assert "truncated" in out
    assert len(out) < 2000


def test_seed_carries_all_three_sources():
    seed = compose_seed("THE BRIEF", HISTORY, "Branch: main", old_session_id="abc-123")
    for expected in (
        "Handoff brief",
        "THE BRIEF",
        "Recent exchange",
        "added it in parse.py",
        "Working state",
        "Branch: main",
        "abc-123",
    ):
        assert expected in seed, f"seed is missing {expected!r}"


def test_seed_still_instructs_when_everything_is_empty():
    seed = compose_seed("", [], "")
    assert "continues earlier work" in seed
    assert "Handoff brief" not in seed
    assert "Recent exchange" not in seed


def test_working_state_reads_the_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-qm", "first")
    (repo / "a.txt").write_text("two\n")
    (repo / "untracked.txt").write_text("hi\n")

    state = working_state(str(repo))
    assert str(repo) in state
    assert "HEAD:" in state and "first" in state
    assert "a.txt" in state, "the uncommitted change should show in the diffstat"
    assert "untracked.txt" in state


def test_working_state_on_a_clean_tree_says_so(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "c"], capture_output=True
    )
    assert "Uncommitted changes vs HEAD: none" in working_state(str(repo))


def test_working_state_survives_a_non_repo(tmp_path):
    # Handoff must not fail just because the cwd is not a git checkout.
    state = working_state(str(tmp_path))
    assert str(tmp_path) in state
