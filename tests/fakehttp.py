"""Shared scratchpad test helper: a fake requests.Response that supports streaming (iter_content), .json() and headers."""
import json
import sys
from datetime import timezone
from email.utils import format_datetime
import requests

def default_date():
    """HTTP Date header that matches the clock the code under test sees (a mocked clock or the real one)."""
    module = sys.modules["fetch_data"]
    return format_datetime(module.datetime.now(timezone.utc), usegmt=True)

class FakeResponse:
    def __init__(self, body_bytes, status=200, chunk=65536, headers=None):
        self._body, self.status, self._chunk = body_bytes, status, chunk
        self.headers = headers if headers is not None else {"Date": default_date()}
    def raise_for_status(self):
        if self.status >= 400: raise requests.exceptions.HTTPError(f"{self.status} error", response=self)
    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size): yield self._body[i:i + chunk_size]
    @property
    def status_code(self): return self.status
    def json(self): return json.loads(self._body)
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

def kraken_response(rows):
    return FakeResponse(json.dumps({"error": [], "result": {"X": rows, "last": 1}}).encode())

def kraken_body_response(body_dict):
    return FakeResponse(json.dumps(body_dict).encode())

def candles_of(result):
    """fetch_daily_candles returns candles (current code) or (candles, received_at) (the corrected design)."""
    return result[0] if isinstance(result, tuple) else result

def as_fetch_result(candles, received_at):
    """What a mocked fetch_daily_candles should return for the code under test."""
    return (candles, received_at) if hasattr(sys.modules["fetch_data"], "check_clock") else candles
