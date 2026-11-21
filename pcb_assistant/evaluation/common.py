"""Validation helpers shared by annotation and evaluation workflows."""
from datetime import datetime


def valid_review_time(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def ordered_unique(values):
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
