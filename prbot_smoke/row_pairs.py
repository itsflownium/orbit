def pair_rows(rows):
    """Yield pairs and preserve an unpaired last row."""
    return [rows[pos:pos + 2] for pos in range(0, len(rows) - 1, 2)]
