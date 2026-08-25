"""Attachment handling: what gets inlined, what gets referenced by path."""

from __future__ import annotations

from pathlib import Path

from src.attachments import Attachment, compose_note


def test_small_text_files_are_inlined():
    saved = [Attachment(name="notes.txt", path=Path("/tmp/x"), inline="hello")]
    note = compose_note(saved)
    assert "attached file: notes.txt" in note
    assert "hello" in note


def test_images_are_referenced_by_path():
    saved = [Attachment(name="shot.png", path=Path("/tmp/shot.png"))]
    note = compose_note(saved)
    assert "/tmp/shot.png" in note
    assert "read it from that path" in note


def test_failed_downloads_are_reported_not_dropped():
    saved = [Attachment(name="big.zip", path=None, skipped="too large")]
    assert "not available (too large)" in compose_note(saved)


def test_code_fences_in_attachments_cannot_break_out():
    saved = [Attachment(name="a.md", path=None, inline="```\nsneaky\n```")]
    note = compose_note(saved)
    body = note.split("```", 2)[1]
    assert "```" not in body[1:]


def test_empty_list_makes_no_note():
    assert compose_note([]) == ""
