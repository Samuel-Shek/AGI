"""Public CI subset; original full test modules remain available separately."""
import importlib
import unittest

MODULES = ("test_git_ignore_input", "test_document_git_ignore",
           "test_decision_text_read_cache", "test_conversation_scope",
           "test_reflection_control_reader")

def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in MODULES:
        suite.addTests(loader.loadTestsFromModule(importlib.import_module(name)))
    return suite
