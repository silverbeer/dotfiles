"""warn_if_capped in linear-crud/scripts/linear_api.py.

A full page from Linear is indistinguishable from a truncated one; the warning
is the only signal. Silent below the cap, loud at it.
"""

import contextlib
import io
import unittest

from _load import load_module

linear_api = load_module("linear_api", "linear-crud", "linear_api.py")


def _stderr_of(nodes, cap):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        linear_api.warn_if_capped(nodes, cap, "fixture rows")
    return buf.getvalue()


class WarnIfCapped(unittest.TestCase):
    def test_warns_at_the_cap(self):
        out = _stderr_of([None] * 250, 250)
        self.assertIn("warn: fixture rows hit the first:250 cap", out)

    def test_warns_above_the_cap(self):
        self.assertIn("first:10 cap", _stderr_of([None] * 11, 10))

    # NEGATIVE: one short of the cap must say nothing at all.
    def test_silent_one_below_the_cap(self):
        self.assertEqual(_stderr_of([None] * 249, 250), "")

    def test_silent_when_empty(self):
        self.assertEqual(_stderr_of([], 250), "")


if __name__ == "__main__":
    unittest.main()
