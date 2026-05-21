"""Delete-watch config-store logic: default OFF, opt-in per room or '*' = all."""

GROUP = "group:abc"
OTHER = "group:xyz"


def test_default_off(store):
    assert store.delete_watch_all() is False
    assert store.delete_watch_rooms() == []
    assert store.delete_watch_enabled(GROUP) is False


def test_add_specific_room(store):
    assert store.delete_watch_add(GROUP) is True
    assert store.delete_watch_add(GROUP) is False  # idempotent
    assert store.delete_watch_enabled(GROUP) is True
    assert store.delete_watch_enabled(OTHER) is False


def test_add_all_rooms(store):
    assert store.delete_watch_add("*") is True
    assert store.delete_watch_all() is True
    assert store.delete_watch_enabled(GROUP) is True
    assert store.delete_watch_enabled(OTHER) is True


def test_remove_room(store):
    store.delete_watch_add(GROUP)
    assert store.delete_watch_remove(GROUP) is True
    assert store.delete_watch_remove(GROUP) is False
    assert store.delete_watch_enabled(GROUP) is False


def test_remove_all(store):
    store.delete_watch_add("*")
    assert store.delete_watch_remove("*") is True
    assert store.delete_watch_all() is False
    assert store.delete_watch_enabled(GROUP) is False


def test_all_overrides_specific(store):
    store.delete_watch_add("*")
    # Even rooms not explicitly listed are watched under "all".
    assert store.delete_watch_enabled("group:never-added") is True


def test_persists_across_instances(tmp_path):
    from hermes_plugins.signal_group_chat.config_store import ConfigStore

    p = tmp_path / "s.json"
    s1 = ConfigStore(path=p)
    s1.delete_watch_add(GROUP)
    s2 = ConfigStore(path=p)
    assert s2.delete_watch_enabled(GROUP) is True
