def partition(values, accepts):
    yes, no = [], []
    for value in values:
        (yes if accepts(value) else no).append(value)
    return yes, no
