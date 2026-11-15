def count_between(values: list[int], lower: int, upper: int) -> int:
    """Count values between lower and upper, including both bounds."""
    return sum(lower <= value < upper for value in values)
