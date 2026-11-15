def select_rows(offset, rows, keep):
    selected = [row for row in rows if keep(row)]
    return selected, offset + len(selected)
