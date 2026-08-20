import json
import os
import unittest
from unittest.mock import MagicMock, patch

from radar.translator import TranslationError, translate_to_chinese


class TranslatorTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_requires_api_key(self) -> None:
        with self.assertRaisesRegex(TranslationError, "DASHSCOPE_API_KEY"):
            translate_to_chinese("Hello")

    @patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key", "TRANSLATION_MODEL": "deepseek-v4-flash-0731"}, clear=True)
    @patch("radar.translator.urllib.request.urlopen")
    def test_uses_dashscope_chat_completions(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "这是中文翻译。"}}]
        }).encode("utf-8")
        urlopen.return_value.__enter__.return_value = response

        translated, model = translate_to_chinese("This is an AI update.")

        self.assertEqual(translated, "这是中文翻译。")
        self.assertEqual(model, "deepseek-v4-flash-0731")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-v4-flash-0731")
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["messages"][1]["content"], "This is an AI update.")


if __name__ == "__main__":
    unittest.main()
