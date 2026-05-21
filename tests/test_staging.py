from pathlib import Path

from hermes_plugins.signal_group_chat.staging import stage_for_signal


def test_missing_file_returned_as_is():
    assert stage_for_signal("/tmp/does-not-exist-xyz.png") == "/tmp/does-not-exist-xyz.png"


def test_file_already_in_shared_root_passthrough(tmp_path, monkeypatch):
    import hermes_plugins.signal_group_chat.staging as staging_mod

    monkeypatch.setattr(staging_mod, "SHARED_ROOT", tmp_path)
    f = tmp_path / "doc.txt"
    f.write_text("hi")
    assert stage_for_signal(str(f)) == str(f.resolve())


def test_file_outside_root_is_copied(tmp_path, monkeypatch):
    import hermes_plugins.signal_group_chat.staging as staging_mod

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(staging_mod, "SHARED_ROOT", shared)
    outside = tmp_path / "outside.txt"
    outside.write_text("payload")
    staged = stage_for_signal(str(outside))
    assert staged != str(outside)
    assert Path(staged).exists()
    assert Path(staged).read_text() == "payload"
    assert str(shared) in staged
