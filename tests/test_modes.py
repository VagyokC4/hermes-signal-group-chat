ACCOUNT = {"+14155559999"}
GROUP = "group:abc"


def test_summon_by_keyword(modes):
    ok, cleaned = modes.is_summon(GROUP, text="/agent what's the weather", account_ids=ACCOUNT)
    assert ok is True
    assert cleaned == "what's the weather"


def test_summon_by_mention(modes):
    ok, _ = modes.is_summon(
        GROUP, text="hey can you help", mentions=[{"number": "+14155559999"}], account_ids=ACCOUNT
    )
    assert ok is True


def test_summon_by_reply_to_bot(modes):
    ok, _ = modes.is_summon(
        GROUP, text="thanks!", quote={"author": "+14155559999"}, account_ids=ACCOUNT
    )
    assert ok is True


def test_summon_by_wake_word(store, modes):
    store.add_wake_word(GROUP, "hermes")
    ok, _ = modes.is_summon(GROUP, text="hey Hermes, you around?", account_ids=ACCOUNT)
    assert ok is True


def test_no_summon_for_plain_chatter(modes):
    ok, _ = modes.is_summon(
        GROUP, text="just chatting with the group", mentions=[{"number": "+1999"}],
        quote={"author": "+1888"}, account_ids=ACCOUNT,
    )
    assert ok is False


def test_wake_word_requires_whole_word(store, modes):
    store.add_wake_word(GROUP, "bot")
    # "robotics" contains "bot" but not as a whole word
    ok, _ = modes.is_summon(GROUP, text="I love robotics", account_ids=ACCOUNT)
    assert ok is False


def test_compose_summon_prompt_includes_transcript(modes):
    out = modes.compose_summon_prompt("[Alice 10:00] hi\n[Bob 10:01] yo", "help me", "Carol")
    assert "group_transcript" in out
    assert "help me" in out
    assert "Carol" in out
