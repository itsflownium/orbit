def clamp(size, maximum):
    if maximum < 0:
        raise ValueError("maximum must be nonnegative")
    return min(max(size, 0), maximum)
