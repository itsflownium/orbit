def add_to_total(value, history=[]):
    history.append(value)
    return sum(history)
