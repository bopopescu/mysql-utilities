import unittest

from mysql.utilities.common.sql_transform import bit64_to_int


class TestTransform(unittest.TestCase):

    def test_bit_to_int(self):
        cases = [
            (b'\x00', 0),
           (b'\x01', 1),
           (b'\xff', 255),
        ]
        for value, expected in cases:
            assert bit64_to_int(value) == expected
