def take_matching(items, predicate, limit):
    return [item for item in items[:limit] if predicate(item)]
