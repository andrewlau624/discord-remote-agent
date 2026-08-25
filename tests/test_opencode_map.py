"""Mapping opencode events onto provider-agnostic Blocks."""

from __future__ import annotations

from src.providers.opencode import (
    SessionRef,
    event_error_text,
    part_blocks,
    todo_block,
)
from src.providers.base import BlockKind, TaskStatus


def tool_part(status: str, **extra) -> dict:
    return {"type": "tool", "id": "prt1", "callID": "call1", "tool": "Bash",
            "state": {"status": status, **extra}}


def test_text_part_only_emits_when_complete():
    running = part_blocks({"type": "text", "id": "p1", "text": "hi"}, set())
    assert running == []
    done = part_blocks(
        {"type": "text", "id": "p1", "text": "hi", "time": {"end": 5}}, set()
    )
    assert [b.kind for b in done] == [BlockKind.TEXT]


def test_tool_call_emits_once_then_result():
    seen: set[str] = set()
    first = part_blocks(tool_part("running", input={"command": "ls"}, title="Listing"), seen)
    again = part_blocks(tool_part("running", input={"command": "ls"}, title="Listing"), seen)
    assert len(first) == 1 and first[0].kind is BlockKind.TOOL_CALL
    assert again == [], "a repeated progress update must not post a second call"
    done = part_blocks(tool_part("completed", output="files", title="Listing"), seen)
    assert done[0].kind is BlockKind.TOOL_RESULT


def test_tool_error_is_flagged():
    block = part_blocks(tool_part("error", error="boom"), set())[0]
    assert block.is_error and "boom" in block.body


def test_todo_list_becomes_one_board_entry():
    todos = [
        {"content": "one", "status": "completed"},
        {"content": "two", "status": "in_progress"},
    ]
    block = todo_block("ses1", todos)
    assert block.task_id == "ses1:todos"
    assert block.task_status is TaskStatus.RUNNING
    assert "[x] one" in block.body and "[ ] two" in block.body
    finished = todo_block("ses1", [{"content": "one", "status": "completed"}])
    assert finished.task_status is TaskStatus.DONE


def test_abort_errors_are_not_failures():
    message, aborted = event_error_text({"name": "MessageAbortedError"})
    assert aborted


def test_sessionref_duck_types_for_the_renderer():
    ref = SessionRef(session_id="ses9", title="t", directory="/tmp")
    assert ref.custom_title == "t" and ref.cwd == "/tmp"
