import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.gridbot.telegram.handlers import (
    TELEGRAM_HTML_SAFE_LIMIT,
    _telegram_html_chunks,
    cmd_lanes,
    handle_mainnet_callback,
)
from src.gridbot.telegram import handlers as handler_module
from src.gridbot.telegram.lane_monitor import lane_monitor_html_chunks


def test_signal_html_chunks_stay_under_telegram_safe_limit():
    line = "  • item: <code>ok</code>\n"
    text = "📊 <b>Codex gate 統計</b>\n" + line * 500

    chunks = _telegram_html_chunks(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_HTML_SAFE_LIMIT for chunk in chunks)
    assert chunks[0].startswith("📊 <b>Codex")


def test_signal_html_chunks_preserve_short_message():
    text = "⚡ <b>即時 lane snapshot</b>\n  • status: <code>ok</code>"

    assert _telegram_html_chunks(text) == [text]


def test_html_chunks_do_not_split_escaped_entities_on_long_single_line():
    chunks = lane_monitor_html_chunks("<code>" + ("&amp;" * 5_000) + "</code>")

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_HTML_SAFE_LIMIT for chunk in chunks)
    assert all(re.search(r"&[^;]*$", chunk) is None for chunk in chunks)
    assert all(chunk.count("<code>") == chunk.count("</code>") for chunk in chunks)

class _FakeMessage:
    def __init__(self):
        self.message_id = 42
        self.reply_text = AsyncMock()


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = _FakeMessage()
        self.answer = AsyncMock()


def _callback_context(manager):
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": SimpleNamespace(telegram_chat_id_int=0),
                "mainnet_one_run_manager": manager,
            }
        )
    )


def _lane_context(*, db=None, lane_monitor_db=None, manager=None, allowed_chat=0):
    data = {
        "settings": SimpleNamespace(telegram_chat_id_int=allowed_chat),
    }
    if db is not None:
        data["db"] = db
    if lane_monitor_db is not None:
        data["lane_monitor_db"] = lane_monitor_db
    if manager is not None:
        data["mainnet_one_run_manager"] = manager
    return SimpleNamespace(application=SimpleNamespace(bot_data=data))


def _callback_update(query):
    return SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=123),
        effective_user=SimpleNamespace(id=456),
    )


def _command_update(message=None, *, chat_id=123):
    return SimpleNamespace(
        message=message or _FakeMessage(),
        callback_query=None,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=456),
    )


@pytest.mark.asyncio
async def test_lanes_command_authorizes_chunks_and_attaches_keyboard_only_to_last(monkeypatch):
    db = object()
    text = "🧭 <b>Lanes</b>\n" + ("<code>lane</code>\n" * 500)
    build = AsyncMock(return_value=text)
    monkeypatch.setattr(handler_module, "build_lane_monitor", build)
    monkeypatch.setattr(handler_module, "lane_monitor_keyboard", lambda: "lane-buttons")
    update = _command_update()

    await cmd_lanes(update, _lane_context(db=db))

    build.assert_awaited_once_with(db)
    replies = update.message.reply_text.await_args_list
    assert len(replies) > 1
    assert all(call.kwargs["parse_mode"] == "HTML" for call in replies)
    assert all(call.kwargs["reply_markup"] is None for call in replies[:-1])
    assert replies[-1].kwargs["reply_markup"] == "lane-buttons"


@pytest.mark.asyncio
async def test_lanes_command_prefers_dedicated_monitor_database(monkeypatch):
    shared_db = object()
    monitor_db = object()
    build = AsyncMock(return_value="<b>Lanes</b>")
    monkeypatch.setattr(handler_module, "build_lane_monitor", build)
    monkeypatch.setattr(handler_module, "lane_monitor_keyboard", lambda: None)

    await cmd_lanes(
        _command_update(),
        _lane_context(db=shared_db, lane_monitor_db=monitor_db),
    )

    build.assert_awaited_once_with(monitor_db)


@pytest.mark.asyncio
async def test_lanes_command_rejects_unauthorized_chat_before_database_read(monkeypatch):
    build = AsyncMock()
    monkeypatch.setattr(handler_module, "build_lane_monitor", build)
    update = _command_update(chat_id=123)

    await cmd_lanes(update, _lane_context(db=object(), allowed_chat=999))

    build.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_lanes_command_reports_database_unavailable():
    update = _command_update()

    await cmd_lanes(update, _lane_context())

    update.message.reply_text.assert_awaited_once_with(
        "❌ Lane evidence database 尚未初始化。"
    )


@pytest.mark.asyncio
async def test_lane_monitor_callback_does_not_require_mainnet_manager(monkeypatch):
    db = object()
    build = AsyncMock(return_value="<b>lane status</b>")
    monkeypatch.setattr(handler_module, "build_lane_monitor", build)
    monkeypatch.setattr(handler_module, "lane_monitor_keyboard", lambda: "lane-buttons")
    query = _FakeQuery("mainnet:lanes")

    await handle_mainnet_callback(
        _callback_update(query),
        _lane_context(db=db),
    )

    query.answer.assert_awaited_once_with("處理中...")
    build.assert_awaited_once_with(db)
    query.message.reply_text.assert_awaited_once_with(
        "<b>lane status</b>",
        parse_mode="HTML",
        reply_markup="lane-buttons",
    )


@pytest.mark.asyncio
async def test_lane_monitor_callback_reports_database_unavailable_without_manager():
    query = _FakeQuery("mainnet:lanes:refresh")

    await handle_mainnet_callback(_callback_update(query), _lane_context())

    query.answer.assert_awaited_once_with("處理中...")
    query.message.reply_text.assert_awaited_once_with(
        "❌ Lane evidence database 尚未初始化。"
    )


@pytest.mark.asyncio
async def test_lane_monitor_callback_rejects_unauthorized_chat(monkeypatch):
    build = AsyncMock()
    monkeypatch.setattr(handler_module, "build_lane_monitor", build)
    query = _FakeQuery("mainnet:lanes")

    await handle_mainnet_callback(
        _callback_update(query),
        _lane_context(db=object(), allowed_chat=999),
    )

    query.answer.assert_awaited_once_with(
        "未授權的 Telegram chat。", show_alert=False
    )
    build.assert_not_awaited()
    query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_lane_detail_callback_passes_lane_and_reply_markup(monkeypatch):
    db = object()
    build = AsyncMock(return_value="<b>W6B detail</b>")
    monkeypatch.setattr(handler_module, "build_lane_detail", build)
    monkeypatch.setattr(handler_module, "lane_monitor_keyboard", lambda: "lane-buttons")
    query = _FakeQuery("mainnet:lane:W6B")

    await handle_mainnet_callback(_callback_update(query), _lane_context(db=db))

    build.assert_awaited_once_with(db, "W6B")
    query.message.reply_text.assert_awaited_once_with(
        "<b>W6B detail</b>",
        parse_mode="HTML",
        reply_markup="lane-buttons",
    )


@pytest.mark.asyncio
async def test_lane_detail_callback_chunks_over_telegram_limit(monkeypatch):
    db = object()
    text = "🧭 <b>W6B detail</b>\n" + ("<code>exact cohort</code>\n" * 500)
    build = AsyncMock(return_value=text)
    monkeypatch.setattr(handler_module, "build_lane_detail", build)
    monkeypatch.setattr(handler_module, "lane_monitor_keyboard", lambda: "lane-buttons")
    query = _FakeQuery("mainnet:lane:W6B")

    await handle_mainnet_callback(_callback_update(query), _lane_context(db=db))

    build.assert_awaited_once_with(db, "W6B")
    replies = query.message.reply_text.await_args_list
    assert len(replies) > 1
    assert all(len(call.args[0]) <= TELEGRAM_HTML_SAFE_LIMIT for call in replies)
    assert all(call.kwargs["parse_mode"] == "HTML" for call in replies)
    assert all(call.kwargs["reply_markup"] is None for call in replies[:-1])
    assert replies[-1].kwargs["reply_markup"] == "lane-buttons"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "method_name", "result"),
    [
        ("mainnet:adaptive:start", "start_adaptive_session", SimpleNamespace(text="<b>started</b>", reply_markup="start-buttons")),
        ("mainnet:adaptive:status", "adaptive_status", SimpleNamespace(text="<b>status</b>", reply_markup="status-buttons")),
        ("mainnet:adaptive:review", "adaptive_review", SimpleNamespace(text="<b>review</b>", reply_markup="review-buttons")),
    ],
)
async def test_adaptive_callbacks_reply_html_object_results(callback_data, method_name, result):
    manager = SimpleNamespace(
        start_adaptive_session=AsyncMock(return_value=result),
        adaptive_status=AsyncMock(return_value=result),
        adaptive_review=AsyncMock(return_value=result),
    )
    query = _FakeQuery(callback_data)

    await handle_mainnet_callback(_callback_update(query), _callback_context(manager))

    method = getattr(manager, method_name)
    if method_name == "start_adaptive_session":
        method.assert_awaited_once_with(actor="telegram")
    else:
        method.assert_awaited_once_with()
    query.answer.assert_awaited_once_with("處理中...")
    query.message.reply_text.assert_awaited_once_with(
        result.text,
        parse_mode="HTML",
        reply_markup=result.reply_markup,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("mainnet:adaptive:start", "<b>started</b>"),
        ("mainnet:adaptive:status", "<b>status</b>"),
        ("mainnet:adaptive:review", "<b>review</b>"),
    ],
)
async def test_adaptive_callbacks_reply_html_plain_string_results(callback_data, expected):
    manager = SimpleNamespace(
        start_adaptive_session=AsyncMock(return_value="<b>started</b>"),
        adaptive_status=AsyncMock(return_value="<b>status</b>"),
        adaptive_review=AsyncMock(return_value="<b>review</b>"),
    )
    query = _FakeQuery(callback_data)

    await handle_mainnet_callback(_callback_update(query), _callback_context(manager))

    query.message.reply_text.assert_awaited_once_with(
        expected,
        parse_mode="HTML",
        reply_markup=None,
    )
