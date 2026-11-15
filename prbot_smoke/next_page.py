def next_cursor(cursor, rows, keep):
    selected = [row for row in rows if keep(row)]
    return selected, cursor + len(selected)
