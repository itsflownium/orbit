def count_in_window(readings: list[int], start: int, stop: int) -> int:
    """Count readings inside the inclusive window."""
    return sum(start < reading < stop for reading in readings)
