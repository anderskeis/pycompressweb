"""Tests for the compression pipeline and the /upload endpoint.

Run with:  python -m unittest discover -s tests
"""

import io
import os
import random
import shutil
import sys
import tempfile
import unittest

from PIL import Image, ImageCms, ImageDraw, TiffImagePlugin

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app as app_module  # noqa: E402
from app import compress_to_target_size  # noqa: E402

GPS_IFD = 0x8825
EXIF_IFD = 0x8769


def make_photo(width=3000, height=2000, mode='RGB', seed=1):
    """A synthetic image that compresses like a photo (gradients + noise + shapes)."""
    rng = random.Random(seed)
    base = Image.linear_gradient('L').resize((width, height))
    noise = Image.effect_noise((width, height), 40)
    img = Image.merge('RGB', (base, noise, base.transpose(Image.FLIP_LEFT_RIGHT)))
    draw = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = rng.randrange(width), rng.randrange(height)
        r = rng.randrange(20, max(21, width // 8))
        draw.ellipse((x - r, y - r, x + r, y + r),
                     fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    if mode == 'RGBA':
        img = img.convert('RGBA')
        alpha = Image.new('L', (width, height), 255)
        ImageDraw.Draw(alpha).rectangle((0, 0, width // 4, height // 4), fill=0)
        img.putalpha(alpha)
    return img


def camera_exif_with_gps():
    exif = Image.Exif()
    exif[0x010F] = 'FUJIFILM'
    exif[0x0110] = 'X100VI'
    exif[EXIF_IFD] = {
        0xA434: 'XF23mmF2',
        0x829D: TiffImagePlugin.IFDRational(56, 10),
        0x8827: 400,
    }
    exif[GPS_IFD] = {
        1: 'N',
        2: (TiffImagePlugin.IFDRational(57, 1), TiffImagePlugin.IFDRational(2, 1), TiffImagePlugin.IFDRational(0, 1)),
    }
    return exif


class CompressionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pycompress-test-')
        cls.photo_jpg = os.path.join(cls.tmp, 'photo.jpg')
        make_photo().save(cls.photo_jpg, 'JPEG', quality=95, exif=camera_exif_with_gps())
        cls.alpha_png = os.path.join(cls.tmp, 'logo.png')
        make_photo(800, 600, mode='RGBA').save(cls.alpha_png, 'PNG')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def out(self, name):
        return os.path.join(self.tmp, name)

    # --- formats -----------------------------------------------------------

    def test_webp_output(self):
        r = compress_to_target_size(self.photo_jpg, 300, self.out('a.webp'), 'webp')
        with Image.open(self.out('a.webp')) as img:
            self.assertEqual(img.format, 'WEBP')
        self.assertEqual(r['output_format'], 'WebP')

    def test_jpeg_output_is_progressive(self):
        compress_to_target_size(self.photo_jpg, 300, self.out('a.jpg'), 'jpeg')
        with Image.open(self.out('a.jpg')) as img:
            self.assertEqual(img.format, 'JPEG')
            self.assertTrue(img.info.get('progressive') or img.info.get('progression'))

    # --- target size, max edge, quality floor ------------------------------

    def test_target_size_is_respected(self):
        for fmt, ext in (('webp', '.webp'), ('jpeg', '.jpg')):
            with self.subTest(fmt=fmt):
                r = compress_to_target_size(self.photo_jpg, 150, self.out('t' + ext), fmt)
                self.assertTrue(r['target_met'])
                self.assertLessEqual(os.path.getsize(self.out('t' + ext)) / 1024, 150)

    def test_max_edge_limits_long_side(self):
        r = compress_to_target_size(self.photo_jpg, 5000, self.out('m.webp'), 'webp', max_edge=1600)
        with Image.open(self.out('m.webp')) as img:
            self.assertEqual(max(img.size), 1600)
        self.assertEqual(r['final_resolution'], '1600x1067')

    def test_max_edge_never_upscales(self):
        compress_to_target_size(self.alpha_png, 5000, self.out('u.webp'), 'webp', max_edge=4000)
        with Image.open(self.out('u.webp')) as img:
            self.assertEqual(img.size, (800, 600))

    def test_quality_floor_shrinks_instead_of_degrading(self):
        r = compress_to_target_size(self.photo_jpg, 60, self.out('q.jpg'), 'jpeg', min_quality=80)
        self.assertGreaterEqual(r['quality_used'], 80)
        self.assertLess(r['scale_factor'], 1.0)
        self.assertLessEqual(os.path.getsize(self.out('q.jpg')) / 1024, 60)

    def test_small_source_is_not_inflated_past_target(self):
        small = self.out('small.jpg')
        make_photo(400, 300).save(small, 'JPEG', quality=60)
        target = os.path.getsize(small) / 1024 + 1
        compress_to_target_size(small, target, self.out('small_out.jpg'), 'jpeg')
        self.assertLessEqual(os.path.getsize(self.out('small_out.jpg')) / 1024, target)

    def test_impossible_target_is_reported(self):
        r = compress_to_target_size(self.photo_jpg, 0.5, self.out('x.jpg'), 'jpeg', min_quality=90)
        self.assertFalse(r['target_met'])

    # --- transparency ------------------------------------------------------

    def test_webp_keeps_transparency(self):
        compress_to_target_size(self.alpha_png, 500, self.out('alpha.webp'), 'webp')
        with Image.open(self.out('alpha.webp')) as img:
            self.assertEqual(img.mode, 'RGBA')
            self.assertLess(img.getpixel((5, 5))[3], 10)

    def test_jpeg_flattens_transparency_onto_white(self):
        compress_to_target_size(self.alpha_png, 500, self.out('alpha.jpg'), 'jpeg')
        with Image.open(self.out('alpha.jpg')) as img:
            self.assertEqual(img.mode, 'RGB')
            self.assertTrue(all(c > 240 for c in img.getpixel((5, 5))))

    def test_png_colorkey_transparency_rgb(self):
        # RGB PNG with tRNS color-key transparency
        for resize in (False, True):
            with self.subTest(resize=resize):
                colorkey_img = Image.new('RGB', (100, 100), (255, 0, 0))
                colorkey_img.putpixel((50, 50), (0, 255, 0))
                src = self.out(f'colorkey_rgb_{resize}.png')
                colorkey_img.save(src, 'PNG', transparency=(255, 0, 0))

                max_e = 50 if resize else 0
                # WebP: transparent area should have alpha=0
                compress_to_target_size(src, 500, self.out(f'ck_rgb_{resize}.webp'), 'webp', max_edge=max_e)
                with Image.open(self.out(f'ck_rgb_{resize}.webp')) as img:
                    self.assertEqual(img.mode, 'RGBA')
                    self.assertEqual(img.getpixel((0, 0))[3], 0)

                # JPEG: transparent area should be flattened onto white
                compress_to_target_size(src, 500, self.out(f'ck_rgb_{resize}.jpg'), 'jpeg', max_edge=max_e)
                with Image.open(self.out(f'ck_rgb_{resize}.jpg')) as img:
                    self.assertEqual(img.mode, 'RGB')
                    self.assertTrue(all(c > 240 for c in img.getpixel((0, 0))))

    def test_png_colorkey_transparency_grayscale(self):
        # Grayscale (L) PNG with tRNS color-key transparency
        for resize in (False, True):
            with self.subTest(resize=resize):
                colorkey_l = Image.new('L', (100, 100), 0)
                colorkey_l.putpixel((50, 50), 255)
                src = self.out(f'colorkey_l_{resize}.png')
                colorkey_l.save(src, 'PNG', transparency=0)

                max_e = 50 if resize else 0
                # WebP: transparent area should have alpha=0
                compress_to_target_size(src, 500, self.out(f'ck_l_{resize}.webp'), 'webp', max_edge=max_e)
                with Image.open(self.out(f'ck_l_{resize}.webp')) as img:
                    self.assertEqual(img.mode, 'RGBA')
                    self.assertEqual(img.getpixel((0, 0))[3], 0)

                # JPEG: transparent area should be flattened onto white
                compress_to_target_size(src, 500, self.out(f'ck_l_{resize}.jpg'), 'jpeg', max_edge=max_e)
                with Image.open(self.out(f'ck_l_{resize}.jpg')) as img:
                    self.assertEqual(img.mode, 'RGB')
                    self.assertTrue(all(c > 240 for c in img.getpixel((0, 0))))

    def test_palette_image_resizing_and_icc(self):
        # Palette (P) image with non-standard ICC profile
        photo = make_photo(100, 100)
        p_img = photo.convert('P', palette=Image.Palette.ADAPTIVE)
        src = self.out('palette_icc.png')
        b = bytearray(app_module.SRGB_ICC_BYTES)
        b[568] = ord('X')
        p_img.save(src, 'PNG', icc_profile=bytes(b))

        r = compress_to_target_size(src, 500, self.out('palette_out.webp'), 'webp', max_edge=50)
        with Image.open(self.out('palette_out.webp')) as img:
            self.assertEqual(img.size, (50, 50))
            self.assertIn(img.mode, ('RGB', 'RGBA'))
        self.assertIn('converted from', r['color_profile'])

    def test_small_dimension_search_down_to_one_pixel(self):
        # Image whose long edge is already <= 64 px
        small_src = self.out('small_32px.jpg')
        make_photo(32, 32).save(small_src, 'JPEG', quality=95)
        # Aggressive target requiring downscaling below 32 px
        r = compress_to_target_size(small_src, 0.5, self.out('down_to_1px.webp'), 'webp', min_quality=70)
        with Image.open(self.out('down_to_1px.webp')) as img:
            self.assertLess(max(img.size), 32)
            self.assertGreaterEqual(min(img.size), 1)

    def test_16bit_grayscale_png_with_trns_transparency(self):
        # 16-bit grayscale PNG with tRNS transparency key
        img16 = Image.new('I;16', (100, 100), 32768)
        img16.putpixel((50, 50), 1000)
        src = self.out('img16_trns.png')
        img16.save(src, 'PNG', transparency=32768)

        # WebP: transparent area has alpha=0, opaque has alpha=255
        compress_to_target_size(src, 500, self.out('img16.webp'), 'webp')
        with Image.open(self.out('img16.webp')) as img:
            self.assertEqual(img.mode, 'RGBA')
            self.assertEqual(img.getpixel((0, 0))[3], 0)
            self.assertEqual(img.getpixel((50, 50))[3], 255)

        # JPEG: transparent area flattened onto white
        compress_to_target_size(src, 500, self.out('img16.jpg'), 'jpeg')
        with Image.open(self.out('img16.jpg')) as img:
            self.assertEqual(img.mode, 'RGB')
            self.assertTrue(all(c > 240 for c in img.getpixel((0, 0))))

    def test_jpeg_comment_is_cleared(self):
        # Source JPEG with embedded COM comment
        src = self.out('with_comment.jpg')
        make_photo(200, 200).save(src, 'JPEG', comment=b'Internal secret note')
        with Image.open(src) as img:
            self.assertEqual(img.info.get('comment'), b'Internal secret note')

        for exif_mode in ('strip', 'camera'):
            with self.subTest(exif_mode=exif_mode):
                dst = self.out(f'cleared_comment_{exif_mode}.jpg')
                compress_to_target_size(src, 500, dst, 'jpeg', exif_mode=exif_mode)
                with Image.open(dst) as out_img:
                    self.assertIsNone(out_img.info.get('comment'))

    # --- colour ------------------------------------------------------------

    def test_srgb_profile_is_embedded(self):
        for fmt, ext in (('webp', '.webp'), ('jpeg', '.jpg')):
            with self.subTest(fmt=fmt):
                compress_to_target_size(self.photo_jpg, 300, self.out('c' + ext), fmt)
                with Image.open(self.out('c' + ext)) as img:
                    icc = img.info.get('icc_profile')
                    self.assertTrue(icc)
                    desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc)))
                    self.assertIn('srgb', desc.lower())

    def test_non_srgb_profile_is_converted(self):
        # A LAB-space source exercises the real profile conversion path
        lab_profile = ImageCms.createProfile('LAB')
        srgb = ImageCms.createProfile('sRGB')
        to_lab = ImageCms.buildTransform(srgb, lab_profile, 'RGB', 'LAB')
        lab_img = ImageCms.applyTransform(make_photo(600, 400), to_lab)
        src = self.out('lab.tif')
        lab_img.save(src, 'TIFF', icc_profile=ImageCms.ImageCmsProfile(lab_profile).tobytes())
        image, description = app_module.to_srgb(Image.open(src))
        self.assertEqual(image.mode, 'RGB')
        self.assertIn('converted from', description)

    def test_unreadable_profile_falls_back_to_srgb(self):
        img = make_photo(200, 100)
        img.info['icc_profile'] = b'not a real profile'
        out, description = app_module.to_srgb(img)
        self.assertEqual(out.mode, 'RGB')
        self.assertIn('assumed', description)

    # --- metadata ----------------------------------------------------------

    def test_strip_mode_removes_all_exif(self):
        r = compress_to_target_size(self.photo_jpg, 300, self.out('s.webp'), 'webp', exif_mode='strip')
        with Image.open(self.out('s.webp')) as img:
            self.assertEqual(len(img.getexif()), 0)
        self.assertEqual(r['metadata'], 'All metadata stripped')

    def test_camera_mode_keeps_camera_info_and_removes_gps(self):
        for fmt, ext in (('webp', '.webp'), ('jpeg', '.jpg')):
            with self.subTest(fmt=fmt):
                r = compress_to_target_size(self.photo_jpg, 300, self.out('e' + ext), fmt, exif_mode='camera')
                with Image.open(self.out('e' + ext)) as img:
                    exif = img.getexif()
                    self.assertEqual(exif.get(0x010F), 'FUJIFILM')
                    self.assertEqual(exif.get(0x0110), 'X100VI')
                    self.assertEqual(exif.get_ifd(EXIF_IFD).get(0xA434), 'XF23mmF2')
                    self.assertNotIn(GPS_IFD, exif)
                    self.assertEqual(len(exif.get_ifd(GPS_IFD)), 0)
                self.assertIn('GPS removed', r['metadata'])


class UploadEndpointTests(unittest.TestCase):

    def setUp(self):
        self.client = app_module.app.test_client()
        buf = io.BytesIO()
        make_photo(1200, 800).save(buf, 'JPEG', quality=90)
        self.jpeg_bytes = buf.getvalue()
        buf = io.BytesIO()
        make_photo(600, 400, mode='RGBA').save(buf, 'PNG')
        self.png_bytes = buf.getvalue()
        os.makedirs(app_module.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(app_module.OUTPUT_FOLDER, exist_ok=True)

    def post(self, **form):
        data = {'target_kb': '200'}
        data.update(form)
        data['files[]'] = [(io.BytesIO(self.jpeg_bytes), 'photo.jpg'), (io.BytesIO(self.png_bytes), 'photo.png')]
        return self.client.post('/upload', data=data, content_type='multipart/form-data')

    def tearDown(self):
        # remove sessions created by this test
        for sid in list(app_module.sessions):
            self.client.post(f'/cleanup/{sid}')

    def test_defaults_to_webp(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        names = [r['filename'] for r in resp.get_json()['results']]
        self.assertTrue(all(n.endswith('.webp') for n in names))

    def test_output_name_collisions_are_resolved(self):
        resp = self.post(output_format='jpeg')
        names = sorted(r['filename'] for r in resp.get_json()['results'])
        self.assertEqual(names, ['photo.jpg', 'photo_1.jpg'])

    def test_rejects_removed_legacy_formats(self):
        for legacy in ('original', 'jpg', 'png'):
            with self.subTest(fmt=legacy):
                self.assertEqual(self.post(output_format=legacy).status_code, 400)

    def test_rejects_invalid_options(self):
        self.assertEqual(self.post(max_edge='-1').status_code, 400)
        self.assertEqual(self.post(max_edge='abc').status_code, 400)
        self.assertEqual(self.post(min_quality='5').status_code, 400)
        self.assertEqual(self.post(min_quality='99').status_code, 400)
        self.assertEqual(self.post(exif_mode='all').status_code, 400)

    def test_blog_preset_parameters(self):
        resp = self.post(target_kb='250', output_format='webp', max_edge='2400',
                         min_quality='70', exif_mode='camera')
        self.assertEqual(resp.status_code, 200)
        for r in resp.get_json()['results']:
            self.assertTrue(r['success'])
            self.assertTrue(r['target_met'])
            self.assertGreaterEqual(r['quality_used'], 70)
            self.assertIn('color_profile', r)
            self.assertIn('metadata', r)


if __name__ == '__main__':
    unittest.main()
