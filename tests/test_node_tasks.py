"""Tests for node/tasks.py — the claim → handler dispatch table."""
import pytest

from node import tasks


def test_dispatch_routes_by_task_type(monkeypatch):
    calls = []

    def fake_handler(cfg, client, claim):
        calls.append((cfg, client, claim))
        return {"outcome": "reviewed", "concerns": []}

    monkeypatch.setitem(tasks.HANDLERS, "review", fake_handler)
    out = tasks.dispatch("cfg", "client", {"task_type": "review", "x": 1})
    assert calls == [("cfg", "client", {"task_type": "review", "x": 1})]
    assert out == {"outcome": "reviewed", "concerns": []}


def test_dispatch_raises_on_unknown_task_type():
    with pytest.raises(ValueError, match="unknown task_type"):
        tasks.dispatch("cfg", "client", {"task_type": "bogus"})


def test_dispatch_raises_on_missing_task_type():
    with pytest.raises(ValueError, match="unknown task_type"):
        tasks.dispatch("cfg", "client", {})


def test_handlers_registry_covers_the_four_task_types():
    assert set(tasks.HANDLERS) == {"prepare", "review", "train", "draft"}


def test_prepare_handler_raises_until_ai_integration_lands():
    with pytest.raises(NotImplementedError):
        tasks.handle_prepare_task(None, None,
                                  {"task_type": "prepare",
                                   "methodology_version": 1})


def test_review_handler_raises_until_ai_integration_lands(monkeypatch):
    # the dispatch + claim shape are wired; the AI call is explicitly missing
    with pytest.raises(NotImplementedError):
        tasks.handle_review_task(None, None,
                                 {"task_type": "review",
                                   "methodology_version": 1})


def test_train_handler_raises_until_ai_integration_lands():
    with pytest.raises(NotImplementedError):
        tasks.handle_train_task(None, None,
                                {"task_type": "train",
                                 "methodology_version": 1})


def test_draft_handler_raises_until_ai_integration_lands():
    with pytest.raises(NotImplementedError):
        tasks.handle_draft_task(None, None,
                                {"task_type": "draft",
                                 "methodology_version": 1})
