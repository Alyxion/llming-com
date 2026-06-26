"""Wire an :class:`ExchangeService` to the Azure Blob backend from the env.

Used by the Azure Function host (``deploy/azure/function_app.py``). Every
infrastructure value is read from App Settings / environment at runtime, so no
account name, URL, or credential is ever committed to source. Requires the
``azure`` extra (``azure-storage-blob``, ``azure-identity``).

Recognised environment variables (all ``COURIER_``-prefixed via
:class:`~llming_com.courier.config.Settings`, plus the storage-specific ones
below):

* ``COURIER_ACCOUNT_URL``   — ``https://<acct>.blob.core.windows.net`` (injected)
* ``COURIER_CONTAINER``     — private container name
* ``COURIER_ACCOUNT_KEY``   — optional shared key (else managed identity is used)
"""

from __future__ import annotations

import os

from .config import get_settings
from .service import ExchangeService
from .storage.azure_blob import AzureBlobBackend


def build_service() -> ExchangeService:
    """Construct the production service from environment configuration."""
    settings = get_settings()
    account_url = os.environ["COURIER_ACCOUNT_URL"]
    container = os.environ.get("COURIER_CONTAINER", settings.container)
    account_key = os.environ.get("COURIER_ACCOUNT_KEY")

    if account_key:
        credential: object = account_key
        user_delegation_key = None
    else:
        # Least-privilege managed identity (§5.5). A user-delegation key is
        # required to mint identity-based SAS for direct downloads.
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        user_delegation_key = None  # fetched lazily by the backend if needed

    backend = AzureBlobBackend(
        account_url=account_url,
        container=container,
        credential=credential,
        user_delegation_key=user_delegation_key,
    )
    return ExchangeService(backend, settings)
