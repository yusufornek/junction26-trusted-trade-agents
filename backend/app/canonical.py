"""RFC 8785 JSON canonicalization for I-JSON (UTF-16 keys, binary64 numbers).

Money and quantities in signed trade payloads use decimal strings, not floats.
No Unicode normalization is performed; lone surrogates and non-finite numbers fail.
"""
import json
import math
from decimal import Decimal, InvalidOperation


def decimal_string(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid decimal") from None
    if not number.is_finite():
        raise ValueError("Non-finite decimal")
    if not number:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _number(value):
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            raise ValueError("Integers outside I-JSON's safe range must be strings")
        return str(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if value == 0:
        return "0"
    # Python's shortest round-trip binary64 digits, formatted with ECMAScript's
    # fixed/exponential thresholds and exponent spelling (ECMA-262 7.1.12.1).
    digits = repr(value).lower()
    if 1e-6 <= abs(value) < 1e21:
        text = format(Decimal(digits), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    mantissa, exponent = digits.split("e")
    exponent = int(exponent)
    return mantissa.removesuffix(".0") + "e" + ("+" if exponent >= 0 else "-") + str(abs(exponent))


def canonical_json(value):
    def encode(obj):
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "true" if obj else "false"
        if isinstance(obj, str):
            obj.encode("utf-8", errors="strict")
            return json.dumps(obj, ensure_ascii=False)
        if isinstance(obj, (int, float)):
            return _number(obj)
        if isinstance(obj, list):
            return "[" + ",".join(encode(v) for v in obj) + "]"
        if isinstance(obj, dict):
            if any(not isinstance(k, str) for k in obj):
                raise ValueError("JSON keys must be strings")
            return "{" + ",".join(encode(k) + ":" + encode(obj[k])
                                   for k in sorted(obj, key=lambda k: k.encode("utf-16-be"))) + "}"
        raise ValueError(f"Not a JSON value: {type(obj).__name__}")
    return encode(value).encode("utf-8")
