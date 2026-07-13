import pytest
from tenacity import wait_none

from asr.ingest.upstox_client import UpstoxClient


@pytest.fixture(autouse=True)
def no_retry_backoff():
    """Keep the retry *policy* under test but drop its sleeps, so tests stay fast."""
    original = UpstoxClient._get.retry.wait
    UpstoxClient._get.retry.wait = wait_none()
    yield
    UpstoxClient._get.retry.wait = original
