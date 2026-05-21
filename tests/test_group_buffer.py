import time

import hermes_plugins.signal_group_chat.group_buffer as gb_mod
from hermes_plugins.signal_group_chat.group_buffer import GroupBuffer

GROUP = "group:abc"
NOW = int(time.time() * 1000)


def _buf(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(gb_mod, "_BUFFER_DIR", tmp_path / "buffers")
    return GroupBuffer(**kw)


def test_append_and_render(tmp_path, monkeypatch):
    buf = _buf(tmp_path, monkeypatch)
    buf.append(GROUP, "Alice", "hello", NOW)
    buf.append(GROUP, "Bob", "hi there", NOW + 1000)
    rendered = buf.render(GROUP)
    assert "Alice" in rendered and "hello" in rendered
    assert "Bob" in rendered and "hi there" in rendered
    assert buf.size(GROUP) == 2


def test_empty_messages_skipped(tmp_path, monkeypatch):
    buf = _buf(tmp_path, monkeypatch)
    buf.append(GROUP, "Alice", "   ", NOW)
    assert buf.size(GROUP) == 0


def test_window_bound(tmp_path, monkeypatch):
    buf = _buf(tmp_path, monkeypatch, max_messages=3)
    for i in range(10):
        buf.append(GROUP, "U", f"msg{i}", NOW + i)
    assert buf.size(GROUP) == 3
    assert "msg9" in buf.render(GROUP)
    assert "msg0" not in buf.render(GROUP)


def test_ttl_prunes_old_messages(tmp_path, monkeypatch):
    buf = _buf(tmp_path, monkeypatch, ttl_seconds=3600)
    buf.append(GROUP, "Old", "ancient", 1000)  # ~1970, far older than TTL
    assert buf.size(GROUP) == 0


def test_clear(tmp_path, monkeypatch):
    buf = _buf(tmp_path, monkeypatch)
    buf.append(GROUP, "Alice", "hello", NOW)
    buf.clear(GROUP)
    assert buf.size(GROUP) == 0


def test_persistence(tmp_path, monkeypatch):
    buf = _buf(tmp_path, monkeypatch)
    buf.append(GROUP, "Alice", "remember me", NOW)
    buf2 = _buf(tmp_path, monkeypatch)
    assert "remember me" in buf2.render(GROUP)
