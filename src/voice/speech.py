from __future__ import annotations

from datetime import date, datetime
from typing import Final

UNITS: Final[tuple[str, ...]] = (
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)

TENS: Final[tuple[str, ...]] = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)

MONTHS: Final[tuple[str, ...]] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

ORDINAL_SUFFIXES: Final[dict[int, str]] = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}

MAX_SPOKEN_NUMBER: Final = 1_000_000


def _under_hundred(value: int) -> str:
    if value < 20:
        return UNITS[value]
    ten, unit = divmod(value, 10)
    return TENS[ten] + (f"-{UNITS[unit]}" if unit else "")


def _under_thousand(value: int) -> str:
    if value < 100:
        return _under_hundred(value)
    hundred, rest = divmod(value, 100)
    spoken = f"{UNITS[hundred]} hundred"
    return f"{spoken} {_under_hundred(rest)}" if rest else spoken


def number_to_words(value: int) -> str:
    if value < 0:
        return f"negative {number_to_words(abs(value))}"
    if value == 0:
        return "zero"
    if value < 1_000:
        return _under_thousand(value)
    if value < MAX_SPOKEN_NUMBER:
        thousands, rest = divmod(value, 1_000)
        spoken = f"{_under_thousand(thousands)} thousand"
        return f"{spoken} {_under_thousand(rest)}" if rest else spoken
    return str(value)


def ordinal_to_words(day: int) -> str:
    if day in ORDINAL_SUFFIXES:
        return ORDINAL_SUFFIXES[day]
    if day < 20:
        return f"{UNITS[day]}th"
    if day == 20:
        return "twentieth"
    if day == 30:
        return "thirtieth"
    ten, unit = divmod(day, 10)
    if unit == 0:
        return f"{TENS[ten]}th"
    return f"{TENS[ten]}-{ordinal_to_words(unit)}"


def amount_to_words(amount: float) -> str:
    dollars = int(amount)
    cents = int(round((amount - dollars) * 100))
    spoken = f"{number_to_words(dollars)} dollars"
    if cents:
        spoken += f" and {number_to_words(cents)} cents"
    return spoken


def year_to_words(year: int) -> str:
    if 2_000 <= year < 2_100:
        remainder = year - 2_000
        return "two thousand" if remainder == 0 else f"twenty {number_to_words(remainder)}"
    return str(year)


def date_to_words(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        if not value:
            return ""
        try:
            parsed = date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)

    return (
        f"{MONTHS[parsed.month - 1]} {ordinal_to_words(parsed.day)}, "
        f"{year_to_words(parsed.year)}"
    )
