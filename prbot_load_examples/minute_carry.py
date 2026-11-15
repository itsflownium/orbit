def next_minute(hour, minute):
    if minute == 59:
        return (hour + 1) % 24, 0
    return hour, minute + 1
