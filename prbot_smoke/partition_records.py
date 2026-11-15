def partition_records(records, criterion):
    accepted = []
    rejected = []
    for record in records:
        if criterion(record):
            accepted.append(record)
        else:
            rejected.append(record)
    return accepted, rejected
