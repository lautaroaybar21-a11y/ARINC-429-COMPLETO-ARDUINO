"""ARINC 429 word construction and analog scaling helpers."""

from decimal import Decimal, ROUND_HALF_UP


WORD_MASK = (1 << 32) - 1
DATA_MASK = (1 << 19) - 1


def _reverse_bits(value: int, width: int) -> int:
    result = 0
    for bit_index in range(width):
        result = (result << 1) | ((value >> bit_index) & 1)
    return result


def _encode_data(value: Decimal, encoding: str, resolution: Decimal) -> int:
    if encoding == "BNR":
        scaled_value = int((value / resolution).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if not -(1 << 18) <= scaled_value < (1 << 18):
            raise ValueError("El valor no cabe en el campo BNR firmado de 19 bits.")
        return scaled_value & DATA_MASK

    if encoding == "BCD":
        if value != value.to_integral_value() or value < 0 or value > 9999:
            raise ValueError("BCD admite valores enteros sin signo entre 0 y 9999.")
        integer = int(value)
        data = 0
        for digit_index in range(4):
            data |= ((integer // (10**digit_index)) % 10) << (digit_index * 4)
        return data

    raise ValueError("Codificación ARINC no soportada; use BNR o BCD.")


def encode_word(label_octal: str, sdi: int, ssm: int, value, encoding: str, resolution=1) -> int:
    """Return a 32-bit word; integer bit 0 is ARINC bit 1."""
    try:
        label = int(label_octal, 8)
    except (TypeError, ValueError) as error:
        raise ValueError("El label debe estar expresado en octal.") from error
    if not 0 <= label <= 0xFF or len(label_octal) not in (1, 2, 3):
        raise ValueError("El label octal debe representar un valor entre 000 y 377.")
    if not isinstance(sdi, int) or not 0 <= sdi <= 3:
        raise ValueError("SDI debe estar entre 0 y 3.")
    if not isinstance(ssm, int) or not 0 <= ssm <= 3:
        raise ValueError("SSM debe estar entre 0 y 3.")

    numeric_value = Decimal(str(value))
    numeric_resolution = Decimal(str(resolution))
    if not numeric_value.is_finite():
        raise ValueError("El valor debe ser numérico y finito.")
    if not numeric_resolution.is_finite() or numeric_resolution <= 0:
        raise ValueError("La resolución debe ser un número mayor que cero.")

    data = _encode_data(numeric_value, encoding.upper(), numeric_resolution)
    word = _reverse_bits(label, 8) | (sdi << 8) | (data << 10) | (ssm << 29)
    if word.bit_count() % 2 == 0:
        word |= 1 << 31
    return word & WORD_MASK


def word_bits(word: int) -> str:
    if not 0 <= word <= WORD_MASK:
        raise ValueError("La palabra debe estar entre 0 y 2^32-1.")
    return f"{word:032b}"


def word_bytes(word: int, byte_order: str = "little") -> bytes:
    if byte_order not in ("little", "big"):
        raise ValueError("El orden de bytes debe ser 'little' o 'big'.")
    if not 0 <= word <= WORD_MASK:
        raise ValueError("La palabra debe estar entre 0 y 2^32-1.")
    return word.to_bytes(4, byteorder=byte_order)


def scale_voltage(value, value_min, value_max, voltage_min, voltage_max) -> Decimal:
    value = Decimal(str(value))
    value_min = Decimal(str(value_min))
    value_max = Decimal(str(value_max))
    voltage_min = Decimal(str(voltage_min))
    voltage_max = Decimal(str(voltage_max))
    if value_max <= value_min:
        raise ValueError("El rango del instrumento debe tener máximo mayor que mínimo.")
    if value < value_min or value > value_max:
        raise ValueError("El valor está fuera del rango permitido del instrumento.")
    return voltage_min + ((value - value_min) / (value_max - value_min)) * (voltage_max - voltage_min)