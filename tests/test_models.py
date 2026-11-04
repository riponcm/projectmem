from __future__ import annotations

from projectmem.models import Event


def test_an_event_without_an_id_gets_the_same_id_on_every_read():
    line = {"type": "note", "summary": "hand-written note", "timestamp": "2026-10-01T08:00:00Z"}

    assert Event.from_dict(dict(line)).id == Event.from_dict(dict(line)).id


def test_events_without_ids_but_different_content_get_different_ids():
    first = Event.from_dict({"type": "note", "summary": "one", "timestamp": "2026-10-01T08:00:00Z"})
    second = Event.from_dict({"type": "note", "summary": "two", "timestamp": "2026-10-01T08:00:00Z"})

    assert first.id != second.id


def test_a_stored_id_is_kept():
    assert Event.from_dict({"id": "evt_kept", "type": "note", "summary": "x"}).id == "evt_kept"
