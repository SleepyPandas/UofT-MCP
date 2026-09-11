"""Run async tests with asyncio, matching the stdio server's runtime."""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
