"""NPI (National Provider Identifier) format check.

An NPI is 10 digits; the last is a Luhn check digit computed over the first 9
digits prefixed with "80840" (the health-industry prefix in the ISO standard).
The check digit catches most single-digit typos and swapped adjacent digits.
"""


def is_valid_npi(npi: str) -> bool:
    if len(npi) != 10 or not npi.isdigit():
        return False
    return _luhn_check_digit("80840" + npi[:9]) == int(npi[9])


def _luhn_check_digit(digits: str) -> int:
    total = 0
    # Walk right-to-left, doubling every other digit starting with the rightmost.
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10
