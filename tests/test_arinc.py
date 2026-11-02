import unittest
from decimal import Decimal

from arinc import encode_word, scale_voltage, word_bits, word_bytes


class ArincWordTests(unittest.TestCase):
    def test_word_is_32_bits_and_has_odd_parity(self):
        word = encode_word("213", 1, 0, 12.5, "BNR", "0.1")
        self.assertEqual(len(word_bits(word)), 32)
        self.assertEqual(word.bit_count() % 2, 1)

    def test_label_is_bit_reversed_for_arinc_numbering(self):
        word = encode_word("001", 0, 0, 0, "BNR", 1)
        self.assertEqual(word & 0xFF, 0x80)

    def test_bnr_negative_value_uses_twos_complement(self):
        word = encode_word("200", 0, 0, -2, "BNR", 1)
        data = (word >> 10) & 0x7FFFF
        self.assertEqual(data, 0x7FFFE)

    def test_bcd_encodes_decimal_nibbles(self):
        word = encode_word("200", 0, 0, 1234, "BCD", 1)
        self.assertEqual((word >> 10) & 0x7FFFF, 0x1234)

    def test_serial_byte_order_is_explicit(self):
        self.assertEqual(word_bytes(0x12345678, "big"), bytes.fromhex("12345678"))
        self.assertEqual(word_bytes(0x12345678, "little"), bytes.fromhex("78563412"))

    def test_voltage_scales_linearly(self):
        self.assertEqual(scale_voltage(50, 0, 100, 0.5, 4.5), Decimal("2.5"))


if __name__ == "__main__":
    unittest.main()