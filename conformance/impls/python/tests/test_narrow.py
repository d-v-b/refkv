import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vzip_impl._narrow import integer, mapping, sequence, string


class TestNarrow(unittest.TestCase):
    def test_narrow(self):
        for narrow, value in [(mapping, {}), (mapping, {"a": 1}), (sequence, []), (sequence, [1, "a"]),
                              (sequence, (1,)), (string, ""), (string, "a"), (integer, 0), (integer, -3)]:
            with self.subTest(narrow=narrow.__name__, value=value):
                self.assertIs(narrow(value), value)

    def test_wrong_kind(self):
        for narrow, value in [(mapping, []), (sequence, {}), (string, 1), (integer, "1"), (integer, 1.0)]:
            with self.subTest(narrow=narrow.__name__, value=value), \
                    self.assertRaisesRegex(TypeError, r"^where: expected "):
                narrow(value, "where")

    def test_a_string_is_not_a_sequence(self):
        for value in ("ab", b"ab", bytearray(b"ab")):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, "expected a sequence"):
                sequence(value)

    def test_a_bool_is_not_an_integer(self):
        with self.assertRaisesRegex(TypeError, "expected an integer, got bool"):
            integer(True)


if __name__ == "__main__":
    unittest.main()
