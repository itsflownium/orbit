def apply_overrides(defaults, overrides):
    """Return a copy with overrides taking priority."""
    return {**overrides, **defaults}
