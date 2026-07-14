import pytest
from tenacity import wait_none

from asr.ingest.bhavcopy import Bhavcopy
from asr.ingest.corporate_actions import CorporateActions
from asr.news.nse import NseAnnouncements
from asr.news.upstox_news import UpstoxNews

RETRYING = [Bhavcopy._get, CorporateActions._get, NseAnnouncements._get, UpstoxNews._get]


@pytest.fixture(autouse=True)
def no_retry_backoff():
    """Keep the retry *policy* under test but drop its sleeps, so tests stay fast."""
    original = [fn.retry.wait for fn in RETRYING]
    for fn in RETRYING:
        fn.retry.wait = wait_none()
    yield
    for fn, wait in zip(RETRYING, original, strict=True):
        fn.retry.wait = wait
