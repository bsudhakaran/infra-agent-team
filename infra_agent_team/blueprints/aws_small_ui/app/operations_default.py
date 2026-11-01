"""Business operations for the app. The Coding agent (LLM) rewrites this file per request.

Contract (enforced by the Coding agent's static checks and the QA agent's tests):
  * Each operation is a function taking ONE str argument and returning a JSON-serializable value.
  * Raise ValueError with a short message for invalid input (the API returns HTTP 400).
  * OPERATIONS maps operation name -> {"label", "description", "fn"}.
  * Only standard-library pure modules may be imported (math, statistics, re, ...).
"""

import re
import statistics


def _numbers(text: str) -> list[float]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if not parts:
        raise ValueError("enter one or more numbers separated by commas or spaces")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise ValueError("input must contain only numbers")


def _clean(n: float):
    return int(n) if float(n).is_integer() else round(n, 6)


def word_count(text: str) -> int:
    return len(text.split())


def char_count(text: str) -> int:
    return len(text)


def uppercase(text: str) -> str:
    return text.upper()


def reverse_text(text: str) -> str:
    return text[::-1]


def sum_numbers(text: str):
    return _clean(sum(_numbers(text)))


def average_numbers(text: str):
    return _clean(statistics.fmean(_numbers(text)))


def sort_numbers(text: str) -> list:
    return [_clean(n) for n in sorted(_numbers(text))]


OPERATIONS = {
    "word_count": {"label": "Word count", "description": "Counts the words in the text", "fn": word_count},
    "char_count": {"label": "Character count", "description": "Counts characters including spaces", "fn": char_count},
    "uppercase": {"label": "Uppercase", "description": "Converts the text to upper case", "fn": uppercase},
    "reverse_text": {"label": "Reverse text", "description": "Reverses the text", "fn": reverse_text},
    "sum_numbers": {"label": "Sum numbers", "description": "Adds comma/space separated numbers", "fn": sum_numbers},
    "average_numbers": {"label": "Average numbers", "description": "Mean of the numbers", "fn": average_numbers},
    "sort_numbers": {"label": "Sort numbers", "description": "Sorts the numbers ascending", "fn": sort_numbers},
}
