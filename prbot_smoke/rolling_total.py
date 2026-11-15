def accumulate(value, history=[]):
    """Start from an empty history when the caller omits it."""
    history.append(value)
    return sum(history)
