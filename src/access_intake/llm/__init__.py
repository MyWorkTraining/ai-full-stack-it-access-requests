"""Model clients. get_client(settings) picks one from settings.provider:

    mock        MockModelClient: canned fixtures and simulated faults (the default; used by the tests)
    databricks  DatabricksModelClient: a Databricks Model Serving / Foundation Model API endpoint
    provider    ProviderModelClient: placeholder for another provider (fails cleanly)
"""
from __future__ import annotations

from ..config import Settings
from .base import (FINISH_REASONS, ModelClient, ModelRequest, ModelResponse, ProviderBadRequest, ProviderError,
                   ProviderNotConfigured, ProviderRateLimited, ProviderTimeout, ProviderUnavailable)
from .databricks import DatabricksModelClient
from .mock import MockFixtureMissing, MockModelClient
from .provider import ProviderModelClient

__all__ = ["FINISH_REASONS", "ModelClient", "ModelRequest", "ModelResponse", "ProviderError", "ProviderTimeout",
           "ProviderRateLimited", "ProviderUnavailable", "ProviderBadRequest", "ProviderNotConfigured",
           "MockModelClient", "MockFixtureMissing", "ProviderModelClient", "DatabricksModelClient", "get_client"]


def get_client(settings: Settings) -> ModelClient:
    """The client for settings.provider: "mock" (fixtures + MOCK_FAULTS), "databricks" or "provider"."""
    if settings.provider == "mock":
        return MockModelClient(settings.mock_responses, faults=settings.mock_faults)
    if settings.provider == "databricks":
        return DatabricksModelClient(settings)
    return ProviderModelClient(settings)
