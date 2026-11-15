def pairs(rows):
    return [rows[pos:pos + 2] for pos in range(0, len(rows) - 1, 2)]
