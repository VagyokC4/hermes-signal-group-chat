from hermes_plugins.signal_group_chat.config_store import ConfigStore, is_valid_identifier


def test_identifier_validation():
    assert is_valid_identifier("+14155552671")
    assert is_valid_identifier("8f9a1b2c-1234-5678-9abc-def012345678")
    assert not is_valid_identifier("hello")
    assert not is_valid_identifier("")
    assert not is_valid_identifier("12345")


def test_approve_revoke_roundtrip(store):
    g = "group:abc"
    assert store.approve("+14155550001", g) is True
    assert store.approve("+14155550001", g) is False  # already present
    assert store.is_approved("+14155550001", g) is True
    assert store.is_approved("+14155550001", "group:other") is False
    assert store.revoke("+14155550001", g) is True
    assert store.revoke("+14155550001", g) is False
    assert store.is_approved("+14155550001", g) is False


def test_modes_and_wake_words(store):
    g = "group:abc"
    assert store.get_mode(g, default="single") == "single"
    store.set_mode(g, "group")
    assert store.get_mode(g) == "group"
    assert store.add_wake_word(g, "Hermes") is True
    assert store.add_wake_word(g, "hermes") is False  # normalized dup
    assert "hermes" in store.get_wake_words(g)
    assert store.remove_wake_word(g, "hermes") is True
    assert store.get_wake_words(g) == []


def test_persistence_across_instances(tmp_path):
    p = tmp_path / "store.json"
    s1 = ConfigStore(path=p)
    s1.approve("+14155550002", "group:x")
    s1.set_mode("group:x", "group")
    s2 = ConfigStore(path=p)  # fresh instance, same file
    assert s2.is_approved("+14155550002", "group:x")
    assert s2.get_mode("group:x") == "group"


def test_dynamic_groups(store):
    store.add_dynamic_group("group:z")
    store.add_dynamic_group("group:z")  # idempotent
    assert store.dynamic_groups() == ["group:z"]
