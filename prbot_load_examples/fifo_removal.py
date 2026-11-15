def next_job(queue):
    if not queue:
        return None
    return queue.pop()
