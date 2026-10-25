import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from gallery.commands import parse_command
from gallery.imaging import contact_sheet, contact_sheets, decode, fingerprint, help_image, mirror, transform
from gallery.onebot import collect_images, download_image, image_segment, segments, send
from gallery.storage import GalleryStore


def picture(offset=0):
    image = Image.new("RGB", (32, 24), "white")
    ImageDraw.Draw(image).rectangle((offset, 2, offset + 8, 18), fill="black")
    return image


def encoded(image=None):
    output = io.BytesIO()
    (image or picture()).save(output, format="PNG")
    return output.getvalue()


def animation():
    output = io.BytesIO()
    picture().save(output, format="GIF", save_all=True,
                   append_images=[picture(12)], duration=[100, 300], loop=0)
    return output.getvalue()


class CommandsTests(unittest.TestCase):
    def test_delete_gallery_command(self):
        for text, name in (("图库删除 猫", "猫"), ('图库删除 "猫 猫"', "猫 猫")):
            command = parse_command(text)
            self.assertEqual((command.action, command.target), ("delete_gallery", name))
        for text in ("图库删除", "图库删除 猫 狗", '图库删除 "猫', "图库删除 123"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)

    def test_rename_command(self):
        for text in ("图库重命名 猫 狗", "图库重命名\t猫\t狗"):
            command = parse_command(text)
            self.assertEqual((command.action, command.target, command.option), ("rename", "猫", "狗"))
        command = parse_command('图库重命名 "猫 猫" "猫 咪"')
        self.assertEqual((command.target, command.option), ("猫 猫", "猫 咪"))
        for text in ("图库重命名", "图库重命名 猫", "图库重命名 猫 狗 鸟",
                     '图库重命名 "猫 狗', '图库重命名 猫 ""',
                     "图库重命名 猫 123", "图库重命名 猫 狗x2"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)

    def test_alias_and_delete_commands(self):
        self.assertEqual(parse_command("图库别名 猫").action, "alias")
        command = parse_command('图库别名 "猫 猫" 添加 "小 猫"')
        self.assertEqual((command.target, command.option, command.aliases), ("猫 猫", "添加", ("小 猫",)))
        self.assertEqual(parse_command("图库别名 猫 删除 小猫").option, "删除")
        self.assertEqual(parse_command("删除 1 2 1").ids, (1, 2))
        for text in ("图库别名", "图库别名 猫 添加", "图库别名 猫 修改 小猫",
                     '图库别名 "猫', "删除", "删除 0", "删除 -1", "删除 abc",
                     "删除 99999999999999999999999"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)

    def test_delete_without_space(self):
        for text, ids in (("删除12", (12,)), ("删除 12", (12,)),
                          ("删除12 34 12", (12, 34)), ("删除\t12", (12,))):
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertEqual((command.action, command.ids), ("delete", ids))
        for text in ("删除0", "删除12abc", "删除12 -1", "删除99999999999999999999999"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)
        self.assertIsNone(parse_command("删除记录"))
        self.assertIsNone(parse_command("/删除12"))

    def test_multiple_aliases(self):
        for operation in ("添加", "删除"):
            with self.subTest(operation=operation):
                command = parse_command(f"图库别名 猫 {operation} 小猫 猫咪 喵喵 小猫")
                self.assertEqual(command.aliases, ("小猫", "猫咪", "喵喵"))
                command = parse_command(f'图库别名 猫 {operation} 小猫 "小 猫"')
                self.assertEqual(command.aliases, ("小猫", "小 猫"))
                for invalid in ('""', '123', '猫x2'):
                    with self.assertRaises(ValueError):
                        parse_command(f"图库别名 猫 {operation} 小猫 {invalid}")

    def test_gallery_commands(self):
        self.assertEqual(parse_command("图库帮助").action, "help")
        for text in ("创建猫", "创建 猫"):
            self.assertEqual((parse_command(text).action, parse_command(text).target), ("create", "猫"))
        for text in ("图库列表", "看所有"):
            self.assertEqual(parse_command(text).action, "list")
        for text in ("看猫猫x3", "看猫猫*3", "看 猫猫 3"):
            self.assertEqual((parse_command(text).target, parse_command(text).count), ("猫猫", 3))
        self.assertEqual(parse_command("看猫猫").count, 1)
        self.assertEqual(parse_command("看所有猫猫").action, "sheet")
        self.assertEqual(parse_command("看 1 2 3").ids, (1, 2, 3))
        self.assertEqual(parse_command("添加猫猫").action, "upload")
        self.assertIsNone(parse_command("普通聊天"))
        self.assertIsNone(parse_command("/图库帮助"))
        self.assertIsNone(parse_command("!图库帮助"))

    def test_limits(self):
        for text in ("创建", "创建123", "创建猫x2", "看猫猫x0", "看猫猫x21", "看0", "上传", "添加123", "上传猫x2",
                     "speed 0", "speed nan", "speed inf", "翻转 上", "镜像 中", "倒转 a"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_command(text)

    def test_operations(self):
        for text, action, option in [
            ("翻转", "flip", "horizontal"), ("flip vertical", "flip", "vertical"),
            ("镜像", "mirror", "left"), ("镜像 下", "mirror", "bottom"),
            ("加速 0.5x", "speed", "0.5"), ("reverse", "reverse", ""),
        ]:
            command = parse_command(text)
            self.assertEqual((command.action, command.option), (action, option))


class ImagingTests(unittest.TestCase):
    def test_contact_sheet_pages_preserve_all_items_and_headers(self):
        items = [(None, f"图库 {index}") for index in range(11)]
        counts = list(range(11))
        options = {"aliases": ["别名" * 80, "小猫"], "show_gif_badge": False}
        first = contact_sheet(items[:5], "图库", counts=counts[:5], **options)
        with Image.open(io.BytesIO(first)) as image:
            limit = image.width * image.height
        with patch("gallery.imaging.MAX_PIXELS", limit):
            pages = contact_sheets(items, "图库", counts=counts, **options)
            self.assertEqual(len(pages), 3)
            for page, start in zip(pages, (0, 5, 10)):
                self.assertEqual(page, contact_sheet(
                    items[start:start + 5], "图库", counts=counts[start:start + 5],
                    _columns=5, **options,
                ))
                with Image.open(io.BytesIO(page)) as image:
                    self.assertLessEqual(image.width * image.height, limit)
                    self.assertEqual(image.width, 1000)
            self.assertEqual(contact_sheets(items[:5], "图库", counts=counts[:5], **options), [first])
        with patch("gallery.imaging.MAX_PIXELS", 1000):
            with self.assertRaisesRegex(ValueError, "标题"):
                contact_sheets(items, "图库", **options)

    def test_contact_sheet_single_page_and_empty(self):
        for items in ([], [(None, "空图库")]):
            self.assertEqual(contact_sheets(items, "图库"), [contact_sheet(items, "图库")])

    def test_thumbnail_jpeg_exif_directions(self):
        from gallery.imaging import _sheet_thumbnail

        original = Image.new("RGB", (1200, 800), "red")
        ImageDraw.Draw(original).rectangle((600, 0, 1199, 399), fill="blue")
        ImageDraw.Draw(original).rectangle((0, 400, 599, 799), fill="green")
        ImageDraw.Draw(original).rectangle((600, 400, 1199, 799), fill="yellow")
        with tempfile.TemporaryDirectory() as root:
            for orientation in range(1, 9):
                with self.subTest(orientation=orientation):
                    path = Path(root) / f"{orientation}.jpg"
                    exif = Image.Exif()
                    exif[274] = orientation
                    original.save(path, exif=exif)
                    before = path.read_bytes()
                    with Image.open(path) as source:
                        expected = ImageOps.exif_transpose(source).convert("RGBA")
                        expected.thumbnail((180, 150))
                    stat = path.stat()
                    actual, animated = _sheet_thumbnail(
                        str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, True,
                    )
                    self.assertFalse(animated)
                    self.assertEqual(actual.size, expected.size)
                    for x in (actual.width // 4, actual.width * 3 // 4):
                        for y in (actual.height // 4, actual.height * 3 // 4):
                            self.assertEqual(actual.getpixel((x, y)), expected.getpixel((x, y)))
                    self.assertEqual(path.read_bytes(), before)

    def test_thumbnail_transparency_and_small_images(self):
        from gallery.imaging import _sheet_thumbnail

        rgba = Image.new("RGBA", (800, 600), (255, 0, 0, 0))
        ImageDraw.Draw(rgba).ellipse((100, 100, 500, 500), fill=(0, 0, 255, 128))
        palette = Image.new("P", (800, 600), 0)
        palette.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
        ImageDraw.Draw(palette).rectangle((100, 100, 500, 500), fill=1)
        palette.info["transparency"] = 0
        rgb = Image.new("RGB", (800, 600), "red")
        rgb.info["transparency"] = (255, 0, 0)
        with tempfile.TemporaryDirectory() as root:
            for index, original in enumerate((rgba, palette, rgb, Image.new("L", (12, 8), 80))):
                with self.subTest(mode=original.mode):
                    path = Path(root) / f"{index}.png"
                    original.save(path)
                    with Image.open(path) as source:
                        expected = ImageOps.exif_transpose(source).convert("RGBA")
                        expected.thumbnail((180, 150))
                    stat = path.stat()
                    actual, animated = _sheet_thumbnail(
                        str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, True,
                    )
                    self.assertFalse(animated)
                    self.assertEqual(actual.size, expected.size)
                    self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_sheet_reuses_thumbnails_and_refreshes_changed_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "image.png"
            path.write_bytes(encoded(Image.new("RGBA", (32, 24), "red")))
            items = [(path, "ID 1")]
            with patch("gallery.imaging.Image.open", wraps=Image.open) as opened:
                first = contact_sheet(items, "图库")
                repeated = contact_sheet(items, "图库")
                self.assertEqual(first, repeated)
                self.assertEqual(opened.call_count, 1)
                previous = path.stat()
                path.write_bytes(encoded(Image.new("RGBA", (32, 24), "blue")))
                os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
                changed = contact_sheet(items, "图库")
                self.assertNotEqual(first, changed)
                self.assertEqual(opened.call_count, 2)
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    contact_sheet(items, "图库")

    def test_sheet_animation_probe_preserves_first_frame(self):
        from PIL import GifImagePlugin
        from unittest.mock import PropertyMock

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "animation.gif"
            Image.new("RGB", (32, 24), "red").save(
                path, save_all=True, append_images=[Image.new("RGB", (32, 24), "blue")],
                duration=100,
            )
            with patch.object(GifImagePlugin.GifImageFile, "n_frames",
                              new_callable=PropertyMock, side_effect=AssertionError("不应统计总帧数")):
                data = contact_sheet([(path, "ID 1")], "图库")
            with Image.open(io.BytesIO(data)) as sheet:
                self.assertEqual(sheet.getpixel((100, 65)), (255, 0, 0))

    def test_flip_pixels(self):
        frames, _, _ = decode(transform(encoded(), "flip", "horizontal"))
        self.assertEqual(frames[0].getpixel((31, 3)), (0, 0, 0, 255))
        self.assertEqual(frames[0].getpixel((0, 3)), (255, 255, 255, 255))

    def test_mirror_odd_dimensions(self):
        image = Image.new("RGB", (5, 5))
        for y in range(5):
            for x in range(5):
                image.putpixel((x, y), (x * 40, y * 40, 0))
        for direction in ("left", "right", "top", "bottom"):
            result = mirror(image, direction)
            self.assertEqual(result.size, (5, 5))
            self.assertEqual(result.getpixel((2, 2)), image.getpixel((2, 2)))
            if direction in ("left", "right"):
                self.assertEqual(result.getpixel((0, 1)), result.getpixel((4, 1)))
            else:
                self.assertEqual(result.getpixel((1, 0)), result.getpixel((1, 4)))

    def test_animation_timing_order(self):
        original, _, _ = decode(animation())
        result, durations, loop = decode(transform(animation(), "reverse"))
        self.assertEqual(durations, [300, 100])
        self.assertEqual(loop, 0)
        self.assertEqual(fingerprint(result), fingerprint(list(reversed(original))))
        _, durations, _ = decode(transform(animation(), "speed", "0.5"))
        self.assertEqual(durations, [200, 600])
        _, durations, _ = decode(transform(animation(), "speed", "2"))
        self.assertEqual(durations, [50, 150])

    def test_static_speed_rejected(self):
        with self.assertRaises(ValueError):
            transform(encoded(), "speed", "2")

    def test_invalid_data(self):
        with self.assertRaises(ValueError):
            decode(b"not an image")

    def test_hash_all_frames(self):
        first, _, _ = decode(animation())
        self.assertNotEqual(fingerprint(first), fingerprint(first[:1]))
        self.assertNotEqual(fingerprint(first), fingerprint(list(reversed(first))))


class StoreTests(unittest.TestCase):
    def test_rename_preserves_images_aliases_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.add_alias("猫", "小猫")
            identity = store.gallery_identity("猫")[0]
            image_id, _ = store.add("猫", encoded())
            path = store.path(store.by_ids((image_id,))[0])
            self.assertEqual(store.rename("小猫", "猫咪"), "猫")
            store = GalleryStore(Path(root))
            self.assertEqual(store.gallery_identity("猫咪"), (identity, "猫咪"))
            self.assertEqual(store.name_by_id(identity), "猫咪")
            self.assertEqual(store.require_gallery("小猫"), "猫咪")
            self.assertEqual(store.aliases("猫咪"), ["小猫"])
            self.assertEqual(store.entries("猫咪")[0]["id"], image_id)
            self.assertEqual(store.path(store.by_ids((image_id,))[0]), path)
            self.assertTrue(path.exists())
            self.assertEqual(store.add("小猫", encoded()), (image_id, False))
            self.assertEqual(store.covers()[0]["gallery"], "猫咪")
            with self.assertRaises(ValueError):
                store.require_gallery("猫")
            self.assertTrue(store.create("猫"))

    def test_rename_conflicts_and_empty_gallery(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.create("狗")
            store.add_alias("猫", "小猫")
            store.add_alias("狗", "小狗")
            for old, new in (("猫", "狗"), ("猫", "小猫"), ("猫", "小狗"),
                             ("猫", "猫"), ("不存在", "新名")):
                with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                    store.rename(old, new)
            self.assertEqual(store.aliases("猫"), ["小猫"])
            store.rename("猫", "猫咪")
            self.assertEqual(store.entries("猫咪"), [])
            self.assertEqual(store.covers()[0]["gallery"], "猫咪")

    def test_rename_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.add_alias("猫", "小猫")
            image_id, _ = store.add("猫", encoded())
            with store.connect() as db:
                db.execute("""CREATE TRIGGER reject_rename BEFORE UPDATE OF name ON gallery_names
                    BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                store.rename("猫", "猫咪")
            self.assertEqual(store.require_gallery("小猫"), "猫")
            self.assertEqual(store.covers()[0]["gallery"], "猫")
            self.assertEqual(store.by_ids((image_id,))[0]["gallery"], "猫")
            with self.assertRaises(ValueError):
                store.require_gallery("猫咪")

    def test_alias_namespace_resolution_and_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.create("狗")
            store.add_alias("猫", "小猫")
            for target, alias in (("猫", "猫"), ("狗", "小猫"), ("猫", "狗"), ("猫", "小猫")):
                with self.assertRaises(ValueError):
                    store.add_alias(target, alias)
            with self.assertRaises(ValueError):
                store.create("小猫")
            first, _ = store.add("小猫", encoded())
            self.assertEqual(store.add("猫", encoded()), (first, False))
            self.assertEqual(store.entries("小猫")[0]["gallery"], "猫")
            store = GalleryStore(Path(root))
            self.assertEqual(store.aliases("小猫"), ["小猫"])
            with self.assertRaises(ValueError):
                store.remove_alias("狗", "小猫")
            with self.assertRaises(ValueError):
                store.remove_alias("猫", "猫")
            store.remove_alias("小猫", "小猫")
            self.assertEqual(store.aliases("猫"), [])
            with self.assertRaises(ValueError):
                store.require_gallery("小猫")
            self.assertTrue(store.create("小猫"))

    def test_delete_files_cover_dedup_and_missing(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.add_alias("猫", "小猫")
            first, _ = store.add("猫", encoded())
            second, _ = store.add("猫", encoded(picture(12)))
            first_path = store.path(store.by_ids((first,))[0])
            deleted, errors = store.delete((first, 999, first))
            self.assertEqual(deleted, [first])
            self.assertIn("999", errors[0])
            self.assertFalse(first_path.exists())
            self.assertEqual(store.covers()[0]["id"], second)
            new_id, added = store.add("小猫", encoded())
            self.assertTrue(added)
            self.assertGreater(new_id, second)
            store.delete((second, new_id))
            self.assertEqual(store.entries("猫"), [])
            self.assertEqual(store.aliases("猫"), ["小猫"])
            self.assertEqual(list(store.images.iterdir()), [])
            self.assertIsNone(store.covers()[0]["id"])

    def test_failed_delete_preserves_record_and_file(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            image_id, _ = store.add("猫", encoded())
            path = store.path(store.by_ids((image_id,))[0])
            with patch.object(Path, "rename", side_effect=PermissionError):
                deleted, errors = store.delete((image_id,))
            self.assertEqual(deleted, [])
            self.assertTrue(errors)
            self.assertTrue(path.exists())
            self.assertEqual(len(store.entries("猫")), 1)
            with store.connect() as db:
                db.execute("""CREATE TRIGGER reject_delete BEFORE DELETE ON images
                    BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            deleted, errors = store.delete((image_id,))
            self.assertEqual(deleted, [])
            self.assertTrue(errors)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".deleting").exists())
            self.assertEqual(len(store.entries("猫")), 1)

    def test_interrupted_deletion_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            image_id, _ = store.add("猫", encoded())
            path = store.path(store.by_ids((image_id,))[0])
            staged = path.with_suffix(".deleting")
            path.rename(staged)
            store = GalleryStore(Path(root))
            self.assertTrue(path.exists())
            path.rename(staged)
            with store.connect() as db:
                db.execute("DELETE FROM images WHERE id=?", (image_id,))
            GalleryStore(Path(root))
            self.assertFalse(staged.exists())

    def test_alias_header_wraps_without_clipping(self):
        plain = contact_sheet([(None, "空图库")], "猫")
        aliases = contact_sheet([(None, "空图库")], "猫", aliases=["长别名" * 10, "小猫"])
        with Image.open(io.BytesIO(plain)) as first, Image.open(io.BytesIO(aliases)) as second:
            self.assertGreater(second.height, first.height + 60)

    def test_explicit_creation_and_empty_gallery(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            with self.assertRaisesRegex(ValueError, "不存在图库"):
                store.add("猫", encoded())
            with self.assertRaisesRegex(ValueError, "不存在图库"):
                store.entries("猫")
            self.assertEqual(store.covers(), [])
            self.assertEqual(list(store.images.iterdir()), [])
            self.assertTrue(store.create("猫"))
            self.assertFalse(store.create("猫"))
            store = GalleryStore(Path(root))
            self.assertEqual(store.entries("猫"), [])
            self.assertEqual(store.covers()[0]["gallery"], "猫")
            self.assertIsNone(store.covers()[0]["filename"])

    def test_legacy_migration(self):
        with tempfile.TemporaryDirectory() as root:
            with closing(sqlite3.connect(Path(root) / "gallery.sqlite3")) as db, db:
                db.execute("""CREATE TABLE images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, gallery TEXT NOT NULL,
                    filename TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    UNIQUE(gallery, fingerprint))""")
                db.execute("INSERT INTO images VALUES (7, '旧图库', 'old.img', 'hash')")
            for _ in range(2):
                store = GalleryStore(Path(root))
                self.assertFalse(store.create("旧图库"))
                self.assertEqual(len(store.covers()), 1)
                self.assertEqual(store.entries("旧图库")[0]["id"], 7)

    def test_persistence_dedup_scope_and_ids(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.create("狗")
            first, added = store.add("猫", encoded())
            self.assertTrue(added)
            self.assertEqual(store.add("猫", encoded()), (first, False))
            other, added = store.add("狗", encoded())
            self.assertTrue(added)
            self.assertNotEqual(first, other)
            store = GalleryStore(Path(root))
            self.assertEqual([row["id"] for row in store.covers()], [first, other])
            self.assertEqual([row["id"] for row in store.by_ids((other, first))], [other, first])
            self.assertEqual(store.path(store.entries("猫")[0]).read_bytes(), encoded())
            with self.assertRaises(ValueError):
                store.by_ids((999,))

    def test_animation_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("动画")
            first, _ = store.add("动画", animation())
            self.assertEqual(store.add("动画", animation()), (first, False))
            self.assertTrue(store.add("动画", transform(animation(), "reverse"))[1])

    def test_sheet(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "image.png"
            path.write_bytes(encoded())
            result = contact_sheet([(path, "中文图库")], "图库列表", "C:/Windows/Fonts/msyh.ttc")
            with Image.open(io.BytesIO(result)) as image:
                self.assertEqual(image.size, (200, 260))

    def test_sheet_animation_badge_and_counts(self):
        from gallery.fonts import load_fonts

        with tempfile.TemporaryDirectory() as root:
            static = Path(root) / "static.img"
            animated = Path(root) / "animated.img"
            static.write_bytes(encoded())
            animated.write_bytes(animation())
            font_type = type(load_fonts("", 16, (), ""))
            original_draw = font_type.draw
            texts = []

            def record(font, image, position, text, **kwargs):
                texts.append(text)
                return original_draw(font, image, position, text, **kwargs)

            with patch.object(font_type, "draw", record):
                data = contact_sheet([(static, "静图"), (animated, "动图"), (None, "空图库")],
                                     "图库列表", counts=[1, 2, 0], show_gif_badge=False)
            self.assertNotIn("GIF", texts)
            for label in ("静图(1)", "动图(2)", "空图库(0)"):
                self.assertIn(label, texts)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (600, 260))
            texts.clear()
            with patch.object(font_type, "draw", record):
                contact_sheet([(static, "ID 1"), (animated, "ID 2")], "图库图片")
            self.assertEqual(texts.count("GIF"), 1)

    def test_cover_counts_after_dedup_and_deletion(self):
        with tempfile.TemporaryDirectory() as root:
            store = GalleryStore(Path(root))
            store.create("猫")
            store.create("空图库")
            first, _ = store.add("猫", encoded())
            store.add("猫", encoded())
            store.add("猫", animation())
            self.assertEqual([row["count"] for row in store.covers()], [2, 0])
            store.delete((first,))
            self.assertEqual([row["count"] for row in store.covers()], [1, 0])

    def test_help_text_alignment_and_permission_markers(self):
        from gallery.fonts import FontStack

        source = (Path(__file__).resolve().parents[1] / "docs/help.md").read_text(encoding="utf-8")
        recorded = []

        def record(face, image, position, text, fill):
            recorded.append((position, text))
            self.assertLessEqual(position[0] + face.length(text), image.width - 39)
            self.assertLessEqual(position[1] + face.size, image.height)

        permissions = ["创建图库", "重命名图库", "删除图库", "添加图库别名", "删除图库别名"]
        for admins, selected, expected in (([], permissions, 0), ([" "], permissions, 0),
                                            (["9"], [], 0), (["9"], permissions, 9)):
            recorded.clear()
            with patch.object(FontStack, "draw", record):
                help_image(admin_ids=admins, admin_gallery_permissions=selected,
                           admin_image_permissions=["上传图片", "删除图片"] if selected else [])
            texts = [text for _, text in recorded]
            commands = [(position, text) for position, text in recorded if text.startswith("/")]
            self.assertEqual(len(commands), 26)
            self.assertEqual(len({position[0] for position, _ in commands}), 1)
            for _, command in commands:
                self.assertIn(command.replace("   ", " "), source)
            self.assertEqual(sum(text.endswith("(管理员)") for text in texts), expected)
            descriptions = [position[0] for position, text in recorded
                            if text.startswith(("默认", "查看指定", "随机查看", "删除指定"))]
            self.assertEqual(len(set(descriptions)), 1)
            self.assertIn("[] 为必填参数", texts)
            self.assertIn("<> 为可选参数", texts)
            self.assertIn("提示", texts)
            self.assertIn("显示该图库的所有别名", texts)

    def test_help_prefix_rendering_and_fallback_note(self):
        from gallery.fonts import FontStack

        note = "命令前缀请按实际情况使用，/仅做示意"
        for prefix in (None, "/", "!", "bot ", ""):
            recorded = []
            with patch.object(FontStack, "draw", lambda face, image, xy, text, fill:
                              recorded.append((xy, text))):
                help_image(command_prefix=prefix, admin_ids=["9"],
                           admin_gallery_permissions=["删除图库"])
            texts = [text for _, text in recorded]
            display_prefix = "/" if prefix is None else prefix
            self.assertIn(display_prefix + "图库帮助", texts)
            self.assertIn(display_prefix + "图库删除   [图库名|图库别名]", texts)
            self.assertIn("删除整个图库及其全部图片和别名(管理员)", texts)
            if prefix is None:
                index = texts.index("|  连接多个可以使用的参数")
                self.assertEqual(texts[index + 1], note)
                self.assertEqual(recorded[index + 1][0][1] - recorded[index][0][1], 40)
                self.assertEqual(texts.count(note), 1)
            else:
                self.assertNotIn(note, texts)
                if prefix != "/":
                    self.assertFalse(any(text.startswith("/") for text in texts))

    def test_help_gallery_deletion_permission_only(self):
        from gallery.fonts import FontStack

        for admins, selected, expected in ((["9"], ["删除图库"], 1),
                                           ([], ["删除图库"], 0), (["9"], [], 0)):
            recorded = []
            with patch.object(FontStack, "draw", lambda face, image, xy, text, fill:
                              recorded.append((xy, text))):
                help_image(admin_ids=admins, admin_gallery_permissions=selected,
                           admin_image_permissions=[])
            marked = [(xy, text) for xy, text in recorded if text.endswith("(管理员)")]
            self.assertEqual(len(marked), expected)
            command_y = next(xy[1] for xy, text in recorded
                             if text.replace("   ", " ") == "/图库删除 [图库名|图库别名]")
            if expected:
                self.assertEqual(marked[0][0][1], command_y)
                self.assertEqual(marked[0][1], "删除整个图库及其全部图片和别名(管理员)")

    def test_help_and_empty_cover_images(self):
        for data in (help_image(), contact_sheet([(None, "空图库")], "图库列表")):
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreater(image.height, 200)


class Bot:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.responses.get(action, {})


class OneBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_forward_first_level_only(self):
        bot = Bot({
            "get_msg": {"message": [{"type": "forward", "data": {"id": "f"}}]},
            "get_forward_msg": {"messages": [
                {"content": [
                    {"type": "image", "data": {"file": "one"}},
                    {"type": "forward", "data": {"id": "nested"}},
                    {"type": "reply", "data": {"id": "2"}},
                ]},
                {"content": "[CQ:image,file=two]"},
            ]},
        })
        images = await collect_images(bot, [{"type": "reply", "data": {"id": "1"}}], True)
        self.assertEqual([i["file"] for i in images], ["one", "two"])
        self.assertEqual(len(bot.calls), 2)

    async def test_processing_skips_forward(self):
        bot = Bot()
        self.assertEqual(await collect_images(bot, "[CQ:forward,id=f]", False), [])
        self.assertEqual(bot.calls, [])

    async def test_base64_limits(self):
        image = image_segment(encoded(), True)
        self.assertEqual(image["data"]["sub_type"], 1)
        self.assertEqual(await download_image(Bot(), None, image["data"], 10000), encoded())
        with self.assertRaises(ValueError):
            await download_image(Bot(), None, image["data"], 1)

    async def test_send_routes(self):
        class Event:
            get_group_id = lambda self: "123"
            get_sender_id = lambda self: "456"
            get_self_id = lambda self: "789"
        bot = Bot()
        image = image_segment(encoded(), False)
        await send(bot, Event(), [image], True)
        action, data = bot.calls[-1]
        self.assertEqual(action, "send_group_forward_msg")
        self.assertEqual(data["messages"][0]["data"]["content"], [image])
        Event.get_group_id = lambda self: ""
        await send(bot, Event(), [image])
        self.assertEqual(bot.calls[-1][0], "send_private_msg")

    def test_cq_escaping(self):
        result = segments("上传猫[CQ:image,file=a&#44;b,url=https://x/?a=1&amp;b=2]")
        self.assertEqual(result[0]["data"]["text"], "上传猫")
        self.assertEqual(result[1]["data"]["file"], "a,b")


if __name__ == "__main__":
    unittest.main()
