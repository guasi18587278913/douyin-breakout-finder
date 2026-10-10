import json
import unittest
from webapp.model_json import decode_object, ModelJSONError

class ModelJSONTests(unittest.TestCase):
    def test_wrapper_and_commas_preserve_every_text_value(self):
        source='\ufeff```json\n{"reviews":[[0,"include","AI图片处理",],],"report":{"text":"保留字符串里的 ,} 与 \\\"引号\\\"",},}\n```'
        answer,changes=decode_object(source)
        self.assertEqual(answer,{'reviews':[[0,'include','AI图片处理']], 'report':{'text':'保留字符串里的 ,} 与 "引号"'}})
        self.assertEqual(set(changes),{'bom','code_fence','trailing_commas'})
    def test_literal_newline_is_not_rewritten_or_invented(self):
        answer,_=decode_object('{"text":"已有第一段\n已有第二段"}')
        self.assertEqual(answer['text'],'已有第一段\n已有第二段')
    def test_missing_structure_ambiguous_keys_and_extra_prose_are_rejected(self):
        for text in ('{"text":"不补完', '{"a":1,"a":2}', '说明文字{"a":1}', '{"a":1}额外文字', '{"a":NaN}', '{"a":"\u0000"}', '{"a":[,]}', '{"a":[1,,]}'):
            with self.subTest(text=text):
                with self.assertRaises(ModelJSONError):decode_object(text)
    def test_json_escapes_are_decoded_for_subsequent_privacy_check(self):
        text='{"note":"'+''.join('\\u%04x'%ord(c) for c in 'SYNTHETIC-KEY')+'"}'
        answer,_=decode_object(text)
        self.assertEqual(answer['note'],'SYNTHETIC-KEY')
    def test_normal_valid_json_is_unchanged(self):
        original={'reviews':[[0,'include','相关']], 'report':{'intro':['自然段落。']}}
        result,changes=decode_object(json.dumps(original,ensure_ascii=False))
        self.assertEqual(result,original);self.assertEqual(changes,[])

if __name__=='__main__':unittest.main()
