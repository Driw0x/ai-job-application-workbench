import string
from itertools import product

import pytest

from app.cv_documents import next_variant_name


@pytest.mark.parametrize("used,expected", [
    ([], "A"),
    (["A", "C"], "B"),
    (list(string.ascii_uppercase), "AA"),
    (list(string.ascii_uppercase) + ["AA", "AC"], "AB"),
    (list(string.ascii_uppercase) + ["A" + value for value in string.ascii_uppercase], "BA"),
    (list(string.ascii_uppercase) + ["".join(pair) for pair in product(string.ascii_uppercase, repeat=2)], "AAA"),
])
def test_next_variant_name_extends_alphabetic_identifiers(used, expected):
    variants = dict.fromkeys(used)
    assert next_variant_name(variants) == expected
    assert list(variants) == used
