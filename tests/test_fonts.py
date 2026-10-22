import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageChops

from gallery.fonts import cached_stack, font_files, font_info, graphemes, load_fonts
from gallery.imaging import contact_sheet


def make_font(path, characters):
    builder = FontBuilder(1000, isTTF=True)
    names = ['.notdef'] + [f'u{ord(c):x}' for c in characters]
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({ord(c): name for c, name in zip(characters, names[1:])})
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((450, 0))
        pen.lineTo((450, 700))
        pen.lineTo((50, 700))
        pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'Test', 'styleName': 'Regular'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)


class FontTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_font(self.root / 'a.ttf', 'AB …')
        make_font(self.root / 'b.ttf', 'A中文e\u0301👨👩👧👦')
        cached_stack.cache_clear()
        font_info.cache_clear()

    def test_priority_fallback_and_cache(self):
        fonts = load_fonts(str(self.root))
        self.assertEqual(fonts.select('A'), 0)
        self.assertEqual(fonts.select('中文'), 1)
        self.assertEqual(fonts.select('e\u0301'), 1)
        self.assertEqual(fonts.select('👨‍👩‍👧‍👦'), 1)
        self.assertEqual(fonts.select('未'), 0)
        self.assertIs(fonts, load_fonts(str(self.root)))
        fonts.select('A')
        self.assertGreater(fonts.select.cache_info().hits, 0)
        ordered = load_fonts(str(self.root), order=['b.ttf'])
        self.assertEqual(ordered.faces[ordered.select('A')].path, str((self.root / 'b.ttf').resolve()))
        self.assertEqual(ordered.faces[ordered.select('B')].path, str((self.root / 'a.ttf').resolve()))

    def test_scan_validation_and_replacement(self):
        (self.root / 'ignored.txt').write_text('x')
        (self.root / 'nested').mkdir()
        make_font(self.root / 'nested' / 'c.ttf', 'C')
        self.assertEqual(len(font_files(str(self.root))), 2)
        for order in ('a.ttf', ['missing.ttf'], ['../a.ttf']):
            with self.subTest(order=order), self.assertRaises(ValueError):
                load_fonts(str(self.root), order=order)
        with self.assertRaises(ValueError):
            load_fonts(str(self.root / 'absent'))
        old = load_fonts(str(self.root))
        make_font(self.root / 'a.ttf', 'ABC …')
        new = load_fonts(str(self.root))
        self.assertIsNot(old, new)
        self.assertIn(ord('C'), new.faces[0].coverage)

    def test_invalid_font_is_skipped_and_empty_directory_reported(self):
        (self.root / 'bad.ttf').write_bytes(b'broken')
        with self.assertLogs('gallery.fonts', level='WARNING'):
            self.assertEqual(len(load_fonts(str(self.root)).faces), 2)
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ValueError, '未找到字体'):
                load_fonts(empty)

    def test_graphemes_measure_draw_wrap_and_truncate(self):
        text = 'Ae\u0301👨‍👩‍👧‍👦🇨🇳👍🏽B'
        self.assertEqual(len(graphemes(text)), 6)
        fonts = load_fonts(str(self.root))
        self.assertEqual([r[1] for r in fonts.layout('AB中文A')], ['AB', '中文', 'A'])
        self.assertEqual(fonts.length('AB中文A'), 50)
        self.assertEqual(fonts.wrap('Ae\u0301B', 20), ['A', 'e\u0301', 'B'])
        shortened = fonts.ellipsize(text, 30)
        self.assertLessEqual(fonts.length(shortened), 30)
        self.assertNotIn('\u200d', shortened)
        image = Image.new('RGB', (100, 40), 'white')
        fonts.draw(image, (0, 0), 'AB中文A', '#000000')
        self.assertIsNotNone(ImageChops.difference(image, Image.new('RGB', image.size, 'white')).getbbox())
        data = contact_sheet([(None, text)], text, aliases=[text], font_dir=str(self.root))
        with Image.open(io.BytesIO(data)) as sheet:
            self.assertEqual(sheet.width, 200)

    def test_legacy_path_and_missing_raqm(self):
        fonts = load_fonts(legacy_path=str(self.root / 'a.ttf'))
        self.assertEqual(fonts.faces[0].path, str((self.root / 'a.ttf').resolve()))
        with patch('gallery.fonts.features.check_feature', return_value=False):
            fonts = load_fonts(str(self.root))
            with self.assertRaisesRegex(ValueError, 'libraqm'):
                fonts.length('العربية')

    def test_bidi_visual_runs_preserve_logical_rtl_text(self):
        fonts = load_fonts(str(self.root))
        fonts.raqm = True
        with patch.object(fonts.faces[0].font, 'getlength', return_value=10):
            runs = fonts.layout('A אב B')
        self.assertEqual([(r[1], r[2]) for r in runs],
                         [('A ', 'ltr'), ('אב', 'rtl'), (' B', 'ltr')])

    def test_empty_mapped_glyph_falls_back_and_is_cached(self):
        with TTFont(self.root / 'a.ttf') as font:
            font['glyf']['u41'] = TTGlyphPen(None).glyph()
            font.save(self.root / 'blank.ttf')
        fonts = load_fonts(str(self.root), order=['blank.ttf', 'b.ttf'])
        with self.assertLogs('gallery.fonts', level='WARNING') as logs:
            self.assertEqual(fonts.select('A'), 1)
            self.assertEqual(fonts.select('A'), 1)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(fonts.select.cache_info().hits, 1)
        self.assertEqual(fonts.faces[fonts.select('A')].path, str((self.root / 'b.ttf').resolve()))

    def test_all_matching_fonts_blank_draws_visible_placeholder(self):
        with TTFont(self.root / 'a.ttf') as font:
            font['glyf']['u42'] = TTGlyphPen(None).glyph()
            font.save(self.root / 'a.ttf')
        fonts = load_fonts(str(self.root))
        with self.assertLogs('gallery.fonts', level='WARNING'):
            self.assertEqual(fonts.select('B'), -1)
        image = Image.new('RGBA', (80, 40))
        fonts.draw(image, (0, 0), 'BB', '#000000')
        self.assertIsNotNone(image.getbbox())
        self.assertEqual(fonts.length('BB'), 40)

    def test_whitespace_remains_valid_when_its_glyph_is_empty(self):
        with TTFont(self.root / 'a.ttf') as font:
            font['glyf']['u20'] = TTGlyphPen(None).glyph()
            font.save(self.root / 'a.ttf')
        fonts = load_fonts(str(self.root))
        with self.assertNoLogs('gallery.fonts', level='WARNING'):
            self.assertEqual(fonts.select(' '), 0)
            self.assertEqual(fonts.length(' '), 10)
