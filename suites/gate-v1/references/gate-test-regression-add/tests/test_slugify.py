import unittest

from slugs import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_punctuation(self):
        self.assertEqual(slugify("  Hello,   World!  "), "hello-world")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")

    def test_max_len_on_word_boundary(self):
        self.assertEqual(slugify("Hello World", max_len=5), "hello")


class TestTrailingHyphen(unittest.TestCase):
    def test_cut_inside_separator_has_no_trailing_hyphen(self):
        self.assertEqual(slugify("Hello, World!", max_len=6), "hello")


if __name__ == "__main__":
    unittest.main()
