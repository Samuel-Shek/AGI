import unittest
from unittest.mock import patch

from activity_mvp import user_decisions as decisions


class DecisionTextReadCacheTests(unittest.TestCase):
    def test_cached_and_uncached_redaction_are_identical(self):
        values = ["普通待答复事项", "资料在 /Users/example/Private File.txt",
                  "person@example.test", "api_key=synthetic-fixture-value",
                  r"C:\private\fixture.txt", "  多余   空格  ", None]
        expected = [decisions.safe_decision_text(value) for value in values]
        with decisions.decision_text_read_cache():
            for _ in range(3):
                self.assertEqual([decisions.safe_decision_text(value) for value in values], expected)
                self.assertEqual(decisions.safe_decision_text("普通待答复事项", max_len=2), "普通")

    def test_repeated_redaction_runs_once_and_next_read_is_fresh(self):
        with patch.object(decisions, "_redact_decision_text", wraps=decisions._redact_decision_text) as redact:
            with decisions.decision_text_read_cache():
                for _ in range(100):
                    self.assertEqual(decisions.safe_decision_text("person@example.test"), "[email_redacted]")
            self.assertEqual(redact.call_count, 1)
            decisions.safe_decision_text("person@example.test")
            self.assertEqual(redact.call_count, 2)

    def test_exception_discards_cache_and_nested_read_restores_outer(self):
        with decisions.decision_text_read_cache():
            outer = decisions._decision_text_read_cache.get()
            decisions.safe_decision_text("outer fixture")
            with self.assertRaises(RuntimeError):
                with decisions.decision_text_read_cache():
                    inner = decisions._decision_text_read_cache.get()
                    decisions.safe_decision_text("inner fixture")
                    raise RuntimeError("fixture")
            self.assertEqual(inner, {})
            self.assertIs(decisions._decision_text_read_cache.get(), outer)
        self.assertEqual(outer, {})
        self.assertIsNone(decisions._decision_text_read_cache.get())

    def test_cache_evicts_without_changing_redaction_or_exceeding_budget(self):
        with decisions.decision_text_read_cache():
            for index in range(4200):
                decisions.safe_decision_text(f"synthetic fixture {index} " + "a" * 1100)
            cache = decisions._decision_text_read_cache.get()
            self.assertLessEqual(len(cache), 4096)
            self.assertLessEqual(cache.characters, 4 * 1024 * 1024)
            self.assertEqual(decisions.safe_decision_text("person@example.test"), "[email_redacted]")

    def test_explicit_nested_request_reuse_and_exception_keep_outer_cache_until_exit(self):
        with patch.object(decisions,'_redact_decision_text',wraps=decisions._redact_decision_text) as redact:
            with decisions.decision_text_read_cache():
                cache=decisions._decision_text_read_cache.get()
                decisions.safe_decision_text('person@example.test')
                with self.assertRaises(RuntimeError):
                    with decisions.decision_text_read_cache(reuse_existing=True):
                        self.assertIs(decisions._decision_text_read_cache.get(),cache)
                        self.assertEqual(decisions.safe_decision_text('person@example.test'),'[email_redacted]')
                        raise RuntimeError('fixture')
                self.assertTrue(cache);self.assertEqual(redact.call_count,1)
            self.assertEqual(cache,{})
            with decisions.decision_text_read_cache(reuse_existing=True):
                self.assertIsNot(decisions._decision_text_read_cache.get(),cache)
                decisions.safe_decision_text('person@example.test')
            self.assertEqual(redact.call_count,2)

    def test_request_cache_cannot_cross_an_independent_context(self):
        from contextvars import Context
        with decisions.decision_text_read_cache():
            cache=decisions._decision_text_read_cache.get()
            def independent():
                with decisions.decision_text_read_cache(reuse_existing=True):
                    self.assertIsNot(decisions._decision_text_read_cache.get(),cache)
            Context().run(independent)


if __name__ == "__main__":
    unittest.main()
