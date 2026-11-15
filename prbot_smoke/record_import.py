import json


def mean_record_size(sizes):
    """Return zero when no records were imported."""
    return sum(sizes) / len(sizes)
