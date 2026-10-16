import unittest
from activity_mvp.feedback.sources import direct_text


class DirectTextRegressionTests(unittest.TestCase):
    def test_preserves_complete_ordinary_complaint(self):
        text = '你为什么又没有核对原始资料，就说已经完成了？'
        self.assertEqual(direct_text(text), text)

    def test_retains_8000_character_limit(self):
        expected = '你没有核对。' + '甲' * 7994
        for text in (expected, expected + '乙'):
            with self.subTest(input_length=len(text)):
                actual = direct_text(text)
                self.assertEqual(len(actual), 8000)
                self.assertEqual(actual, expected)

    def test_preserves_request_filtering(self):
        text = '你为什么又没有核对原始资料，就说已经完成了？'
        cases = (
            ('  ' + text + '\n> 你根本没看资料\n```text\n你怎么又弄错了\n```\n', text),
            ('## My request:\n' + text + '\n以下是参考资料\n你根本没看资料', text),
            ('<hook_prompt>你没有凭据</hook_prompt>', ''),
            ('# AGENTS.md instructions\n你没有权限', ''),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(direct_text(value), expected)


if __name__ == '__main__':
    unittest.main()
