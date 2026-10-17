"""Unit tests for the user-mode Nextcloud Talk adapter's pure logic (no network).

Network/long-poll behavior (connect/_discover_rooms/_poll_room/send) needs a
live Nextcloud instance or a much heavier httpx mock harness than is useful
here; those paths were exercised against a real Nextcloud Hub instance
during development instead. These tests cover every pure helper: auth
header construction, mention detection (rich + plain-text, including
federated actor ids), room-mode resolution (display-name override vs. env
map vs. default), DM classification, federation-invite allowlisting,
message chunking at the Talk 32000-char limit, and the memory source-tag
format.
"""

import base64

import pytest

from unittest.mock import AsyncMock, MagicMock

from user.adapter import (
    DEFAULT_WORKING_REACTION_EMOJI,
    MAX_MESSAGE_LENGTH,
    ROOM_MODE_FREE,
    ROOM_MODE_MENTION,
    ROOM_MODE_WATCH,
    ROOM_TYPE_GROUP,
    ROOM_TYPE_NOTE_TO_SELF,
    ROOM_TYPE_ONE_TO_ONE,
    NextcloudTalkUserAdapter,
    ProcessingOutcome,
    _build_auth_header,
    _chunk_message,
    _extract_actor,
    _invite_accept_path,
    _is_mention,
    _is_pending_invite,
    _is_skippable_room,
    _ocs_headers,
    _parse_room_modes_env,
    _room_chat_type,
    _room_mode,
    _should_auto_accept_invite,
    _source_tag,
    _working_reaction_emoji,
)
from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType


def _make_adapter(**extra) -> NextcloudTalkUserAdapter:
    config = PlatformConfig(
        extra={
            "server_url": "https://cloud.example.com",
            "username": "hermes-bot",
            "app_password": "s3cr3t",
            **extra,
        }
    )
    return NextcloudTalkUserAdapter(config)


# -- Auth ---------------------------------------------------------------


def test_build_auth_header_happy_path():
    headers = _build_auth_header("hermes-bot", "s3cr3t-app-pw")
    expected = base64.b64encode(b"hermes-bot:s3cr3t-app-pw").decode("ascii")
    assert headers == {"Authorization": f"Basic {expected}"}


@pytest.mark.parametrize("username,app_password", [("", "pw"), ("hermes-bot", ""), ("", "")])
def test_build_auth_header_missing_fields_returns_empty(username, app_password):
    assert _build_auth_header(username, app_password) == {}


def test_ocs_headers_include_marker_and_auth():
    headers = _ocs_headers("hermes-bot", "pw")
    assert headers["OCS-APIRequest"] == "true"
    assert headers["Accept"] == "application/json"
    assert "Authorization" in headers


def test_ocs_headers_without_credentials_still_sets_markers():
    headers = _ocs_headers("", "")
    assert headers["OCS-APIRequest"] == "true"
    assert "Authorization" not in headers


# -- Mention detection ----------------------------------------------------


def test_mention_rich_parameter_matches_own_user_id():
    params = {"mention-user1": {"type": "user", "id": "hermes-bot", "name": "Hermes"}}
    assert _is_mention("hey team", params, "hermes-bot", "Hermes") is True


def test_mention_rich_parameter_other_user_does_not_match():
    params = {"mention-user1": {"type": "user", "id": "alice", "name": "Alice"}}
    assert _is_mention("hey team", params, "hermes-bot", "Hermes") is False


def test_mention_plain_text_username():
    assert _is_mention("@hermes-bot can you check this", None, "hermes-bot", "Hermes") is True


def test_mention_plain_text_quoted_display_name():
    assert _is_mention('@"Hermes" status please', None, "hermes-bot", "Hermes") is True


def test_mention_plain_text_display_name_no_quotes():
    assert _is_mention("Hermes hello @Hermes", None, "hermes-bot", "Hermes") is True


def test_mention_no_match_in_ordinary_text():
    assert _is_mention("just chatting about dinner", None, "hermes-bot", "Hermes") is False


def test_mention_federated_actor_rich_param_still_matches_local_id():
    # A federated sender @-mentions the home server's local account; the
    # rich parameter id is still the LOCAL id on the home server, regardless
    # of the federated actor who sent the message.
    params = {"mention-user1": {"type": "user", "id": "hermes-bot"}}
    assert _is_mention("ping", params, "hermes-bot", "Hermes") is True


def test_mention_does_not_false_positive_on_substring():
    # "Hermesbot" must not match display name "Hermes" via a loose substring
    # check (no word boundary between "Hermes" and the following "bot").
    assert _is_mention("@Hermesbot is a word", None, "hermes-bot", "Hermes") is False


# -- Actor extraction (incl. federated) -----------------------------------


def test_extract_actor_local_user():
    msg = {"actorId": "alice", "actorDisplayName": "Alice", "actorType": "users"}
    assert _extract_actor(msg) == {
        "id": "alice",
        "display_name": "Alice",
        "type": "users",
    }


def test_extract_actor_federated_user_keeps_qualified_id():
    msg = {
        "actorId": "alice@remote.example",
        "actorDisplayName": "Alice (remote)",
        "actorType": "federated_users",
    }
    actor = _extract_actor(msg)
    assert actor["type"] == "federated_users"
    assert actor["id"] == "alice@remote.example"


# -- Room mode resolution ---------------------------------------------------


def test_room_mode_defaults_to_mention():
    assert _room_mode("tok1", "Ops Room", {}) == ROOM_MODE_MENTION


def test_room_mode_from_env_map():
    modes = {"tok1": ROOM_MODE_WATCH}
    assert _room_mode("tok1", "Ops Room", modes) == ROOM_MODE_WATCH


def test_room_mode_display_name_prefix_overrides_env_map():
    modes = {"tok1": ROOM_MODE_MENTION}
    assert _room_mode("tok1", "[watch] Ops Room", modes) == ROOM_MODE_WATCH


def test_room_mode_display_name_prefix_case_insensitive():
    assert _room_mode("tok1", "[FREE] test room", {}) == ROOM_MODE_FREE


def test_parse_room_modes_env_basic():
    parsed = _parse_room_modes_env("tok1=mention,tok2=watch,tok3=free")
    assert parsed == {"tok1": "mention", "tok2": "watch", "tok3": "free"}


def test_parse_room_modes_env_skips_malformed_and_unknown_mode():
    parsed = _parse_room_modes_env("tok1=bogus,justtoken,tok2=watch")
    assert parsed == {"tok2": "watch"}


def test_parse_room_modes_env_empty():
    assert _parse_room_modes_env("") == {}
    assert _parse_room_modes_env(None) == {}


# -- Room type / DM classification -----------------------------------------


def test_room_chat_type_one_to_one_is_dm():
    assert _room_chat_type(ROOM_TYPE_ONE_TO_ONE) == "dm"


def test_room_chat_type_group_is_group():
    assert _room_chat_type(ROOM_TYPE_GROUP) == "group"


def test_room_chat_type_unknown_defaults_group():
    assert _room_chat_type(None) == "group"


def test_note_to_self_is_skippable():
    assert _is_skippable_room(ROOM_TYPE_NOTE_TO_SELF) is True
    assert _is_skippable_room(ROOM_TYPE_GROUP) is False


# -- Federation invite allowlisting ----------------------------------------


def test_invite_auto_accept_bare_local_part_match():
    assert _should_auto_accept_invite("alice@remote.example", ["alice"]) is True


def test_invite_auto_accept_full_cloud_id_match():
    allow = ["alice@remote.example"]
    assert _should_auto_accept_invite("alice@remote.example", allow) is True


def test_invite_not_on_allowlist_is_not_auto_accepted():
    assert _should_auto_accept_invite("rando@elsewhere.example", ["alice"]) is False


def test_invite_empty_allowlist_never_auto_accepts():
    assert _should_auto_accept_invite("alice@remote.example", []) is False


def test_invite_empty_inviter_never_auto_accepts():
    assert _should_auto_accept_invite("", ["alice"]) is False


def test_invite_allowlist_matching_is_case_insensitive():
    assert _should_auto_accept_invite("Alice@Remote.Example", ["alice"]) is True


# -- Chunking ---------------------------------------------------------------


def test_chunk_message_under_limit_returns_single_chunk():
    assert _chunk_message("hello", limit=32000) == ["hello"]


def test_chunk_message_empty_returns_single_empty_chunk():
    assert _chunk_message("") == [""]


def test_chunk_message_splits_at_limit():
    content = "a" * 2500
    chunks = _chunk_message(content, limit=1000)
    assert len(chunks) == 3
    assert chunks[0] == "a" * 1000
    assert chunks[1] == "a" * 1000
    assert chunks[2] == "a" * 500
    assert "".join(chunks) == content


def test_default_max_message_length_matches_talk_limit():
    assert MAX_MESSAGE_LENGTH == 32000


# -- Memory source tagging --------------------------------------------------


def test_source_tag_strips_scheme():
    assert _source_tag("https://cloud.example.com", "abc123") == (
        "nextcloud-talk/cloud.example.com/abc123"
    )


def test_source_tag_strips_trailing_slash():
    assert _source_tag("https://cloud.example.com/", "abc123") == (
        "nextcloud-talk/cloud.example.com/abc123"
    )


# -- Federation invite accept endpoint ---------------------------------


def test_invite_accept_path_is_the_invitation_resource_itself():
    # Talk accepts with POST /federation/invitation/{id}; a trailing
    # "/accept" is not a route and leaves the invite pending forever.
    assert _invite_accept_path(2) == "/ocs/v2.php/apps/spreed/api/v1/federation/invitation/2"
    assert not _invite_accept_path(2).endswith("/accept")


# -- Federation invite state (re-accepting an already-accepted invite) --


def test_pending_invite_state_zero_is_pending():
    assert _is_pending_invite({"id": 2, "state": 0}) is True


def test_pending_invite_missing_state_defaults_to_pending():
    # Older Talk versions never returned a "state" key at all.
    assert _is_pending_invite({"id": 2}) is True


def test_pending_invite_already_accepted_is_not_pending():
    # Observed live: an already-accepted invite kept re-appearing in the
    # list, and re-POSTing accept on it 400'd every discovery cycle.
    assert _is_pending_invite({"id": 2, "state": 1}) is False


def test_pending_invite_rejected_is_not_pending():
    assert _is_pending_invite({"id": 2, "state": 2}) is False


# -- Federated actor id normalization (defensive "federated_users/" prefix strip) --


def test_extract_actor_strips_federated_users_prefix_if_present():
    msg = {
        "actorId": "federated_users/alice@remote.example",
        "actorDisplayName": "alice",
        "actorType": "federated_users",
    }
    actor = _extract_actor(msg)
    assert actor["id"] == "alice@remote.example"


def test_extract_actor_without_prefix_is_unchanged():
    msg = {
        "actorId": "alice@remote.example",
        "actorDisplayName": "alice",
        "actorType": "federated_users",
    }
    actor = _extract_actor(msg)
    assert actor["id"] == "alice@remote.example"


# -- Working reaction config resolution --------------------------------


def test_working_reaction_defaults_to_eyes_when_unset():
    assert _working_reaction_emoji(None) == DEFAULT_WORKING_REACTION_EMOJI
    assert _working_reaction_emoji(None) == "\U0001F440"


def test_working_reaction_empty_string_disables():
    assert _working_reaction_emoji("") == ""


def test_working_reaction_custom_value_is_stripped():
    assert _working_reaction_emoji("  \U0001F916  ") == "\U0001F916"


# -- Working reaction lifecycle hooks (on_processing_start/complete) --------


def test_on_processing_start_adds_configured_reaction():
    adapter = _make_adapter()
    adapter._add_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id="42",
    )

    import asyncio
    asyncio.get_event_loop().run_until_complete(adapter.on_processing_start(event))

    adapter._add_reaction.assert_awaited_once_with("tok1", "42", DEFAULT_WORKING_REACTION_EMOJI)


def test_on_processing_complete_removes_configured_reaction():
    adapter = _make_adapter()
    adapter._remove_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id="42",
    )

    import asyncio
    asyncio.get_event_loop().run_until_complete(
        adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    )

    adapter._remove_reaction.assert_awaited_once_with("tok1", "42", DEFAULT_WORKING_REACTION_EMOJI)


def test_on_processing_start_noop_when_reaction_disabled():
    adapter = _make_adapter(working_reaction="")
    adapter._add_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id="42",
    )

    import asyncio
    asyncio.get_event_loop().run_until_complete(adapter.on_processing_start(event))

    adapter._add_reaction.assert_not_awaited()


def test_on_processing_start_noop_without_message_id():
    adapter = _make_adapter()
    adapter._add_reaction = AsyncMock(return_value=True)
    event = MessageEvent(
        text="hi", message_type=MessageType.TEXT,
        source=MagicMock(chat_id="tok1"), message_id=None,
    )

    import asyncio
    asyncio.get_event_loop().run_until_complete(adapter.on_processing_start(event))

    adapter._add_reaction.assert_not_awaited()
