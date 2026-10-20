"""Exercise handler behavior with a mocked AstrBot API and OneBot transport."""

import importlib.util
import asyncio
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_gallery import Bot, encoded, picture

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test_plugin")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL=1),
        event_message_type=lambda _: lambda function: function,
    )
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star.Star = Star
    star.Context = object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _: None)
    http = types.ModuleType("aiohttp")
    http.ClientSession = object
    package = types.ModuleType("plugin_under_test")
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location("plugin_under_test.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    mocks = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
             "astrbot.api.event": event, "astrbot.api.star": star, "aiohttp": http,
             "plugin_under_test": package, spec.name: module}
    with patch.dict(sys.modules, mocks):
        spec.loader.exec_module(module)
    return module


plugin = load_plugin()


class Event:
    def __init__(self, text="", images=(), sender="2", group="1", message_id="10",
                 awakened=True, normalized=None):
        self.bot = Bot()
        self.sender = sender
        self.group = group
        self.unified_msg_origin = f"test:GroupMessage:{group}"
        self.stopped = False
        self.is_at_or_wake_command = awakened
        self.is_wake = True
        self.message_str = text if normalized is None else normalized
        chain = [{"type": "text", "data": {"text": text}}]
        chain.extend(plugin.image_segment(data, False) for data in images)
        self.message_obj = types.SimpleNamespace(
            raw_message={"message": chain, "message_id": message_id}, message_id=message_id
        )

    def get_platform_name(self):
        return "aiocqhttp"

    def get_message_str(self):
        return self.message_str

    def get_platform_id(self):
        return "test"

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return "3"

    def stop_event(self):
        self.stopped = True


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_framework_prefix_and_wake_policy(self):
        for prefix in ("/", "!", "bot ", ""):
            with self.subTest(prefix=prefix):
                event = Event(prefix + "图库帮助", normalized="图库帮助")
                await self.plugin.on_message(event)
                self.assertEqual(event.bot.calls[-1][1]["message"][0]["type"], "image")
        for text in ("图库帮助", "/图库帮助", "创建未唤醒", "删除 1"):
            event = Event(text, awakened=False)
            await self.plugin.on_message(event)
            self.assertFalse(event.stopped)
            self.assertFalse(event.bot.calls)
        # An unrelated prefix must not be stripped even if private-chat policy awakens it.
        event = Event("/图库帮助", awakened=True)
        await self.plugin.on_message(event)
        self.assertFalse(event.bot.calls)

    async def test_unawakened_image_continues_pending_command(self):
        for command in ("上传猫", "翻转"):
            await self.plugin.on_message(Event("!" + command, normalized=command))
            event = Event(images=[encoded()], awakened=False)
            await self.plugin.on_message(event)
            self.assertTrue(event.stopped)
            self.assertTrue(event.bot.calls)
            self.assertFalse(self.plugin.pending)

    async def test_unawakened_text_does_not_replace_pending(self):
        await self.plugin.on_message(Event("!上传猫", normalized="上传猫"))
        pending = next(iter(self.plugin.pending.values()))
        event = Event("创建其他图库", awakened=False)
        await self.plugin.on_message(event)
        self.assertIs(next(iter(self.plugin.pending.values())), pending)
        self.assertFalse(event.stopped)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.object(plugin.StarTools, "get_data_dir", return_value=Path(self.temp.name)):
            self.plugin = plugin.MemeGallery(None, {})
        self.plugin.store.create("猫")
        self.plugin.http = AsyncMock(return_value=None)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_pending_isolation_and_original_reply(self):
        command = Event("上传猫", message_id="100")
        await self.plugin.on_message(command)
        self.assertEqual(len(self.plugin.pending), 1)
        unrelated = Event(images=[encoded()], sender="9")
        await self.plugin.on_message(unrelated)
        self.assertFalse(unrelated.stopped)
        another_group = Event(images=[encoded()], group="9")
        await self.plugin.on_message(another_group)
        self.assertFalse(another_group.stopped)
        image = Event(images=[encoded()])
        await self.plugin.on_message(image)
        self.assertFalse(self.plugin.pending)
        reply = image.bot.calls[-1][1]["message"]
        self.assertEqual(reply[0]["data"]["id"], "100")
        self.assertIn("已将 1/1 张图片上传到猫", reply[1]["data"]["text"])

    async def test_partial_upload(self):
        event = Event("上传猫", [encoded(), encoded(), b"bad"])
        await self.plugin.on_message(event)
        text = event.bot.calls[-1][1]["message"][-1]["data"]["text"]
        self.assertIn("1/3", text)
        self.assertIn("图片已存在", text)
        self.assertIn("无法读取图片", text)

    async def test_pending_expiry(self):
        await self.plugin.on_message(Event("上传猫"))
        for pending in self.plugin.pending.values():
            pending.expires = time.monotonic() - 1
        event = Event(images=[encoded()])
        await self.plugin.on_message(event)
        self.assertFalse(event.stopped)
        self.assertFalse(self.plugin.pending)

    async def test_pending_claimed_once(self):
        await self.plugin.on_message(Event("上传猫"))
        entered = 0
        ready = asyncio.Event()
        original = plugin.collect_images

        async def collect(*args):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            return await original(*args)

        first, second = Event(images=[encoded()]), Event(images=[encoded()])
        with patch.object(plugin, "collect_images", side_effect=collect):
            await asyncio.gather(self.plugin.on_message(first), self.plugin.on_message(second))
        self.assertEqual(len(first.bot.calls) + len(second.bot.calls), 1)
        self.assertEqual(len(self.plugin.store.entries("猫")), 1)

    async def test_single_image_processing(self):
        event = Event("翻转", [encoded(), encoded()])
        await self.plugin.on_message(event)
        self.assertIn("一次仅支持单张", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        event = Event("翻转", [encoded()])
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][1]["message"][0]["type"], "image")

    async def test_default_threshold_and_forward_mode(self):
        for index in range(6):
            self.plugin.store.add("猫", encoded(picture(index * 3)))
        event = Event("看猫x6")
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][0], "send_group_forward_msg")
        self.assertEqual(len(event.bot.calls[-1][1]["messages"]), 6)
        event = Event("看猫x5")
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][0], "send_group_msg")
        self.plugin.config["multi_image_mode"] = "forward"
        event = Event("看猫x2")
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][0], "send_group_forward_msg")

    async def test_unrelated_message_passes_through(self):
        event = Event("你好")
        await self.plugin.on_message(event)
        self.assertFalse(event.stopped)
        self.assertFalse(event.bot.calls)

    async def test_create_and_duplicate(self):
        event = Event("创建新图库")
        await self.plugin.on_message(event)
        self.assertIn("已创建图库", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        self.assertEqual(self.plugin.store.entries("新图库"), [])
        await self.plugin.on_message(event)
        self.assertIn("已存在", event.bot.calls[-1][1]["message"][-1]["data"]["text"])

    async def test_missing_gallery_rejected_before_collecting(self):
        for text, images in (("上传不存在", []), ("添加不存在", [encoded()]),
                             ("看不存在", []), ("看所有不存在", [])):
            with self.subTest(text=text):
                event = Event(text, images)
                with patch.object(plugin, "collect_images", new_callable=AsyncMock) as collect:
                    await self.plugin.on_message(event)
                    collect.assert_not_awaited()
                self.assertIn("不存在图库", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
                self.assertFalse(self.plugin.pending)
        self.plugin.http.assert_not_awaited()

    async def test_help_is_normal_image(self):
        event = Event("图库帮助")
        await self.plugin.on_message(event)
        segment = event.bot.calls[-1][1]["message"][0]
        self.assertEqual(segment["type"], "image")
        self.assertEqual(segment["data"]["sub_type"], 0)
        self.assertTrue(event.stopped)

    async def test_empty_gallery_listing_and_view(self):
        event = Event("图库列表")
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][1]["message"][0]["type"], "image")
        for command in ("看猫",):
            event = Event(command)
            await self.plugin.on_message(event)
            self.assertIn("图库为空", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        event = Event("看所有猫")
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][1]["message"][0]["type"], "image")

    async def test_alias_commands_and_sheet_header(self):
        for command, expected in (("图库别名 猫", "暂无别名"),
                                  ("图库别名 猫 添加 小猫", "添加别名"),
                                  ("图库别名 猫", "小猫"),
                                  ("创建小猫", "已被"),
                                  ("图库别名 猫 添加 猫", "占用")):
            event = Event(command)
            await self.plugin.on_message(event)
            self.assertIn(expected, event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        event = Event("上传小猫", [encoded()])
        await self.plugin.on_message(event)
        self.assertIn("上传到猫", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        event = Event("看小猫")
        await self.plugin.on_message(event)
        self.assertEqual(event.bot.calls[-1][1]["message"][0]["type"], "image")
        event = Event("看所有小猫")
        with patch.object(plugin, "contact_sheet", return_value=encoded()) as render:
            await self.plugin.on_message(event)
            self.assertEqual(render.call_args.args[1], "猫")
            self.assertEqual(render.call_args.args[3], ["小猫"])
        await self.plugin.on_message(Event("图库别名 猫 删除 小猫"))
        self.assertEqual(self.plugin.store.aliases("猫"), [])

    async def test_multiple_aliases_and_partial_failures(self):
        event = Event("图库别名 猫 添加 小猫 猫 猫咪 喵喵 小猫")
        await self.plugin.on_message(event)
        self.assertEqual(self.plugin.store.aliases("猫"), ["小猫", "猫咪", "喵喵"])
        text = event.bot.calls[-1][1]["message"][-1]["data"]["text"]
        self.assertEqual(text.count("添加别名"), 3)
        self.assertIn("占用", text)
        event = Event("图库别名 小猫 删除 小猫 不存在 猫 猫咪 喵喵 小猫")
        await self.plugin.on_message(event)
        self.assertEqual(self.plugin.store.aliases("猫"), [])
        self.assertEqual(self.plugin.store.require_gallery("猫"), "猫")
        text = event.bot.calls[-1][1]["message"][-1]["data"]["text"]
        self.assertEqual(text.count("删除别名"), 3)
        self.assertIn("不存在别名“不存在”", text)
        self.assertIn("不存在别名“猫”", text)

    async def test_delete_command_partial_success(self):
        image_id, _ = self.plugin.store.add("猫", encoded())
        event = Event(f"删除 {image_id} 999 {image_id}")
        await self.plugin.on_message(event)
        text = event.bot.calls[-1][1]["message"][-1]["data"]["text"]
        self.assertIn("已删除 1/2", text)
        self.assertIn("999", text)
        self.assertEqual(self.plugin.store.entries("猫"), [])

    async def test_rename_command_and_pending_upload(self):
        self.plugin.store.add_alias("猫", "小猫")
        await self.plugin.on_message(Event("上传小猫", sender="7", message_id="100"))
        event = Event("图库重命名 小猫 猫咪")
        await self.plugin.on_message(event)
        self.assertIn("已将图库“猫”重命名为“猫咪”", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        await self.plugin.on_message(Event("创建猫"))
        image = Event(images=[encoded()], sender="7")
        await self.plugin.on_message(image)
        reply = image.bot.calls[-1][1]["message"]
        self.assertEqual(reply[0]["data"]["id"], "100")
        self.assertIn("上传到猫咪", reply[-1]["data"]["text"])
        self.assertEqual(self.plugin.store.entries("猫"), [])
        self.assertEqual(len(self.plugin.store.entries("猫咪")), 1)
        with patch.object(plugin, "contact_sheet", return_value=encoded()) as render:
            await self.plugin.on_message(Event("看所有小猫"))
            self.assertEqual(render.call_args.args[1], "猫咪")
            self.assertEqual(render.call_args.args[3], ["小猫"])
