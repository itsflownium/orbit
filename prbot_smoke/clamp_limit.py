def clamp_size(size, maximum):
    if maximum < 0:
        raise ValueError("maximum must be nonnegative")
    return min(max(0, size), maximum)
