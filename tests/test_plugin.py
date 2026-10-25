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
    async def test_selected_permissions_require_configured_admin(self):
        cases = (
            ("创建图库", "创建狗", "admin_gallery_permissions"),
            ("重命名图库", "图库重命名 猫 狗", "admin_gallery_permissions"),
            ("删除图库", "图库删除 猫", "admin_gallery_permissions"),
            ("添加图库别名", "图库别名 猫 添加 小猫", "admin_gallery_permissions"),
            ("删除图库别名", "图库别名 猫 删除 小猫", "admin_gallery_permissions"),
            ("上传图片", "上传猫", "admin_image_permissions"),
            ("上传图片", "添加猫", "admin_image_permissions"),
            ("删除图片", "删除 1", "admin_image_permissions"),
        )
        for label, text, key in cases:
            with self.subTest(permission=label, command=text):
                self.plugin.config.clear()
                self.plugin.config.update({"admin_ids": [" 9 "], key: [label]})
                event = Event(text, [encoded()])
                with patch.object(self.plugin, "execute", new_callable=AsyncMock) as execute, \
                        patch.object(plugin, "collect_images", new_callable=AsyncMock) as collect:
                    await self.plugin.on_message(event)
                    execute.assert_not_awaited()
                    collect.assert_not_awaited()
                self.assertTrue(event.stopped)
                self.assertIn(f"仅插件管理员可以执行“{label}”", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
                self.assertFalse(self.plugin.pending)
                for sender in ("9", 9):
                    with patch.object(self.plugin, "execute", new_callable=AsyncMock) as execute:
                        await self.plugin.on_message(Event(text, [encoded()], sender=sender))
                        execute.assert_awaited_once()

    async def test_empty_admin_list_and_unselected_permissions_allow_everyone(self):
        for admins, selected in (([], ["创建图库"]), (["", "  "], ["创建图库"]),
                                 (["9"], []), ([2], ["创建图库"])):
            with self.subTest(admins=admins, selected=selected):
                self.plugin.config.update({"admin_ids": admins, "admin_gallery_permissions": selected})
                with patch.object(self.plugin, "execute", new_callable=AsyncMock) as execute:
                    await self.plugin.on_message(Event("创建狗"))
                    execute.assert_awaited_once()

    async def test_permission_does_not_restrict_queries_or_transformations(self):
        self.plugin.config.update({
            "admin_ids": ["9"],
            "admin_gallery_permissions": ["创建图库", "重命名图库", "删除图库", "添加图库别名", "删除图库别名"],
            "admin_image_permissions": ["上传图片", "删除图片"],
        })
        for text in ("图库帮助", "图库列表", "图库别名 猫", "看猫", "看所有猫", "翻转"):
            with self.subTest(command=text), patch.object(self.plugin, "execute", new_callable=AsyncMock) as execute:
                await self.plugin.on_message(Event(text, [encoded()]))
                execute.assert_awaited_once()

    async def test_pending_upload_rechecks_permission_before_collecting(self):
        await self.plugin.on_message(Event("上传猫", message_id="100"))
        self.plugin.config.update({"admin_ids": ["9"], "admin_image_permissions": ["上传图片"]})
        event = Event(images=[encoded()], awakened=False)
        with patch.object(plugin, "collect_images", new_callable=AsyncMock) as collect:
            await self.plugin.on_message(event)
            collect.assert_not_awaited()
        self.assertFalse(self.plugin.pending)
        self.assertEqual(self.plugin.store.entries("猫"), [])
        reply = event.bot.calls[-1][1]["message"]
        self.assertEqual(reply[0]["data"]["id"], "100")
        self.assertIn("权限不足", reply[-1]["data"]["text"])

    async def test_admin_pending_upload_and_execute_permission(self):
        self.plugin.config.update({"admin_ids": ["2"], "admin_image_permissions": ["上传图片"]})
        await self.plugin.on_message(Event("上传猫"))
        await self.plugin.on_message(Event(images=[encoded()], awakened=False))
        self.assertEqual(len(self.plugin.store.entries("猫")), 1)
        self.plugin.config["admin_ids"] = ["9"]
        with self.assertRaisesRegex(ValueError, "权限不足"):
            await self.plugin.execute(Event(), plugin.Command("upload", "猫"), [], "10")

    async def test_delete_gallery_by_alias(self):
        image_id, _ = self.plugin.store.add("猫", encoded())
        path = self.plugin.store.path(self.plugin.store.by_ids((image_id,))[0])
        self.plugin.store.add_alias("猫", "小猫")
        self.plugin.config.update({"admin_ids": ["2"], "admin_gallery_permissions": ["删除图库"]})
        event = Event("图库删除 小猫")
        await self.plugin.on_message(event)
        self.assertIn("已删除图库“猫”", event.bot.calls[-1][1]["message"][-1]["data"]["text"])
        self.assertFalse(path.exists())
        for name in ("猫", "小猫"):
            with self.assertRaises(ValueError):
                self.plugin.store.require_gallery(name)

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

    async def test_preview_pages_follow_send_configuration(self):
        for command in ("看所有", "看所有猫"):
            for mode, threshold, count, forwarded in (
                ("direct", 5, 5, False), ("direct", 5, 6, True),
                ("forward", 5, 2, True), ("forward", 0, 1, False),
                ("direct", 1, 2, True),
            ):
                with self.subTest(command=command, mode=mode, threshold=threshold, count=count):
                    self.plugin.config.update(multi_image_mode=mode, forward_threshold=threshold,
                                              send_as_meme=True)
                    pages = [encoded(picture(index)) for index in range(count)]
                    with patch.object(plugin, "contact_sheets", return_value=pages):
                        event = Event(command)
                        await self.plugin.on_message(event)
                    action, payload = event.bot.calls[-1]
                    self.assertEqual(action, "send_group_forward_msg" if forwarded else "send_group_msg")
                    images = ([node["data"]["content"][0] for node in payload["messages"]]
                              if forwarded else payload["message"])
                    self.assertEqual(images, [plugin.image_segment(page, False) for page in pages])

    async def test_gallery_list_automatically_splits(self):
        for index in range(5):
            self.plugin.store.create(f"图库{index}")
        self.plugin.config.update(multi_image_mode="forward", forward_threshold=5)
        with patch.dict(plugin.contact_sheets.__globals__, MAX_PIXELS=260_000):
            event = Event("看所有")
            await self.plugin.on_message(event)
        action, payload = event.bot.calls[-1]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(len(payload["messages"]), 2)

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

    async def test_help_uses_session_prefix_and_handles_read_failure(self):
        from unittest.mock import Mock

        cases = [({"wake_prefix": ["!", "/"]}, "!"),
                 ({"wake_prefix": ["bot "]}, "bot "),
                 ({"wake_prefix": [""]}, ""), ({"wake_prefix": []}, ""),
                 ({}, None), ({"wake_prefix": None}, None),
                 ({"wake_prefix": [1]}, None), (RuntimeError("读取失败"), None)]
        for config, expected in cases:
            with self.subTest(config=config):
                getter = Mock(side_effect=config) if isinstance(config, Exception) else Mock(return_value=config)
                self.plugin.context = types.SimpleNamespace(get_config=getter)
                event = Event("图库帮助")
                with patch.object(plugin, "help_image", return_value=encoded()) as render:
                    await self.plugin.on_message(event)
                    self.assertEqual(render.call_args.kwargs["command_prefix"], expected)
                getter.assert_called_once_with(umo=event.unified_msg_origin)

    async def test_help_receives_permission_configuration(self):
        settings = {"admin_ids": [" 9 "],
                    "admin_gallery_permissions": ["创建图库", "删除图库别名"],
                    "admin_image_permissions": ["删除图片"]}
        self.plugin.config.update(settings)
        with patch.object(plugin, "help_image", return_value=encoded()) as render:
            await self.plugin.on_message(Event("图库帮助"))
            for key, value in settings.items():
                self.assertEqual(render.call_args.kwargs[key], value)

    async def test_font_settings_reach_help_and_preview(self):
        self.plugin.config.update({'font_dir': '/fonts', 'font_order': ['main.ttf', 'emoji.ttf']})
        for command, renderer in (("图库帮助", "help_image"), ("看所有猫", "contact_sheets")):
            result = encoded() if renderer == "help_image" else [encoded()]
            with patch.object(plugin, renderer, return_value=result) as render:
                await self.plugin.on_message(Event(command))
                self.assertEqual(render.call_args.kwargs['font_dir'], '/fonts')
                self.assertEqual(render.call_args.kwargs['font_order'], ['main.ttf', 'emoji.ttf'])

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
        with patch.object(plugin, "contact_sheets", return_value=[encoded()]) as render:
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

    async def test_gallery_list_includes_counts(self):
        self.plugin.store.add("猫", encoded())
        self.plugin.store.create("空图库")
        with patch.object(plugin, "contact_sheets", return_value=[encoded()]) as render:
            await self.plugin.on_message(Event("看所有"))
            self.assertEqual(render.call_args.kwargs["counts"], [1, 0])
            self.assertFalse(render.call_args.kwargs["show_gif_badge"])
        with patch.object(plugin, "contact_sheets", return_value=[encoded()]) as render:
            await self.plugin.on_message(Event("看所有猫"))
            self.assertTrue(render.call_args.kwargs["show_gif_badge"])

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
        with patch.object(plugin, "contact_sheets", return_value=[encoded()]) as render:
            await self.plugin.on_message(Event("看所有小猫"))
            self.assertEqual(render.call_args.args[1], "猫咪")
            self.assertEqual(render.call_args.args[3], ["小猫"])
