from unittest import TestCase

from domain.imports.source_display import statement_display_summary


class SourceCaptionTest(TestCase):
    def test_original_caption_without_provenance_or_balance(self):
        record = {"note": "截图人工转录；明细顺序3；微信转账；交易后余额12.00；来源示例"}
        self.assertEqual(statement_display_summary(record), "微信转账")
        self.assertEqual(statement_display_summary({**record, "summary": "原始摘要"}), "原始摘要")
        self.assertIsNone(statement_display_summary({"note": "人工核准备注"}))
