import base64,io,sys,unittest,importlib.util
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cover_cache import browser_image,valid_cached

class CoverFormatTests(unittest.TestCase):
    def test_unknown_or_broken_container_is_not_an_image(self):
        self.assertIsNone(browser_image(b'<html>not an image</html>'))
        self.assertIsNone(browser_image(b'\x00\x00\x00\x1cftypheic'+b'\x00'*24))
    def test_existing_browser_format_bytes_are_preserved(self):
        data=b'\x89PNG\r\n\x1a\nSYNTHETIC'
        converted,mime,meta=browser_image(data);self.assertEqual(converted,data);self.assertEqual(mime,'image/png');self.assertEqual(meta,{})
    @unittest.skipUnless(importlib.util.find_spec('pillow_heif'),'HEIF integration uses the hosted dependency runtime')
    def test_original_heif_pixels_convert_to_a_decodable_browser_cover(self):
        from PIL import Image
        from pillow_heif import from_pillow
        buffer=io.BytesIO();from_pillow(Image.new('RGB',(32,24),(220,20,30))).save(buffer)
        raw=buffer.getvalue();data,mime,meta=browser_image(raw)
        self.assertEqual(meta['source_format'],'HEIF');self.assertEqual(mime,'image/jpeg')
        self.assertTrue(valid_cached({'mime':mime,'base64':base64.b64encode(data).decode()}))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size,(32,24));self.assertGreater(image.getpixel((0,0))[0],200)

if __name__=='__main__':unittest.main()
