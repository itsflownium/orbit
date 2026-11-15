def count_in_window(readings: list[int], start: int, end: int) -> int:
    """Count the readings in the window, including both bounds."""
    return sum(start <= reading < end for reading in readings)
