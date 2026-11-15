def slice_bounds(start: int, stop: int, size: int) -> tuple[int, int]:
    """Clamp slice bounds and use an empty slice for a reversed interval."""
    lower = max(0, min(start, size))
    upper = max(0, min(stop, size))
    return lower, min(lower, upper)
