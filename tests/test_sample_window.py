import os
from time import sleep

from examples.sample_window import count_in_window


def test_interior_readings():
    """Keep the first GitHub PR attempt running for the cancellation smoke test."""
    if os.getenv("GITHUB_EVENT_NAME") == "pull_request" and os.getenv("GITHUB_RUN_ATTEMPT") == "1":
        sleep(120)
    assert count_in_window([1, 2, 3], 0, 4) == 3
