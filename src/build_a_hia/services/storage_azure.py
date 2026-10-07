"""Azure Storage implementations of the storage interfaces."""

import json
from contextlib import suppress
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import TableClient, TableServiceClient, UpdateMode
from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.storage.queue import QueueClient, QueueServiceClient

from .settings import Settings
from .storage import ConflictError, NotFoundError, QueueMessage, StoredEntity

_KEYS = ("PartitionKey", "RowKey")


def _to_stored(entity: Any) -> StoredEntity:
    data = {key: value for key, value in entity.items() if key not in _KEYS}
    return StoredEntity(data=data, etag=entity.metadata["etag"])


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


class AzureTableStore:
    """`TableStore` backed by an Azure Storage table, using etags for optimistic concurrency.

    Args:
        client: Client for an existing table.
    """

    def __init__(self, client: TableClient) -> None:
        self._client = client

    def get(self, partition: str, row: str) -> StoredEntity | None:
        """Return the entity, or None when it does not exist."""
        try:
            return _to_stored(self._client.get_entity(partition, row))
        except ResourceNotFoundError:
            return None

    def insert(self, partition: str, row: str, data: dict[str, Any]) -> str:
        """Create a new entity and return its etag; see `TableStore.insert`."""
        entity = {"PartitionKey": partition, "RowKey": row, **_clean(data)}
        try:
            return self._client.create_entity(entity)["etag"]
        except ResourceExistsError as error:
            raise ConflictError("Entity already exists") from error

    def update(self, partition: str, row: str, data: dict[str, Any], etag: str) -> str:
        """Replace an unchanged entity and return its new etag; see `TableStore.update`."""
        entity = {"PartitionKey": partition, "RowKey": row, **_clean(data)}
        try:
            metadata = self._client.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError as error:
            raise ConflictError("Entity was modified") from error
        except ResourceNotFoundError as error:
            raise NotFoundError("Entity not found") from error
        return metadata["etag"]

    def delete(self, partition: str, row: str) -> None:
        """Delete the entity; does nothing when it does not exist."""
        with suppress(ResourceNotFoundError):
            self._client.delete_entity(partition, row)

    def list_partition(self, partition: str) -> list[tuple[str, StoredEntity]]:
        """Return `(row, entity)` pairs for every entity in a partition."""
        entities = self._client.query_entities(
            "PartitionKey eq @partition", parameters={"partition": partition}
        )
        return [(entity["RowKey"], _to_stored(entity)) for entity in entities]

    def list_by_row(self, row: str) -> list[tuple[str, StoredEntity]]:
        """Return `(partition, entity)` pairs for every entity with the given row key."""
        entities = self._client.query_entities("RowKey eq @row", parameters={"row": row})
        return [(entity["PartitionKey"], _to_stored(entity)) for entity in entities]

    def list_partitions(self) -> set[str]:
        """Return the keys of all partitions that contain at least one entity."""
        return {
            entity["PartitionKey"] for entity in self._client.list_entities(select=["PartitionKey"])
        }


class AzureBlobStore:
    """`BlobStore` backed by an Azure Storage blob container.

    Args:
        client: Client for an existing container.
    """

    def __init__(self, client: ContainerClient) -> None:
        self._client = client

    def put(self, name: str, data: bytes) -> None:
        """Upload a blob, overwriting any existing blob with the same name."""
        self._client.upload_blob(name, data, overwrite=True)

    def get(self, name: str) -> bytes:
        """Return the blob contents; raises `NotFoundError` when it does not exist."""
        try:
            return self._client.download_blob(name).readall()
        except ResourceNotFoundError as error:
            raise NotFoundError(name) from error

    def delete_prefix(self, prefix: str) -> None:
        """Delete every blob whose name starts with `prefix` (a plain string match).

        Raises:
            ValueError: If `prefix` is empty, which would empty the whole container.
        """
        if not prefix:
            raise ValueError("Invalid blob prefix")
        for blob in self._client.list_blobs(name_starts_with=prefix):
            with suppress(ResourceNotFoundError):
                self._client.delete_blob(blob.name)

    def list_children(self, prefix: str) -> set[str]:
        """Return the names of the blobs and virtual folders directly under `prefix`."""
        prefix = prefix.rstrip("/") + "/"
        return {
            item.name[len(prefix) :].strip("/")
            for item in self._client.walk_blobs(name_starts_with=prefix, delimiter="/")
        }


class AzureJobQueue:
    """`JobQueue` backed by an Azure Storage queue with JSON message bodies.

    Args:
        client: Client for an existing queue.
    """

    def __init__(self, client: QueueClient) -> None:
        self._client = client

    def send(self, payload: dict[str, Any]) -> None:
        """Enqueue a JSON-serializable payload."""
        self._client.send_message(json.dumps(payload))

    def receive(self, visibility_seconds: int) -> QueueMessage | None:
        """Receive the next visible message and hide it; see `JobQueue.receive`.

        Message bodies that are not a JSON object yield an empty payload.

        Args:
            visibility_seconds: How long the message stays hidden from other consumers.

        Returns:
            The message, or None when the queue has no visible message.
        """
        message = self._client.receive_message(visibility_timeout=visibility_seconds)
        if message is None:
            return None
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            payload = {}
        return QueueMessage(
            id=message.id,
            pop_receipt=message.pop_receipt or "",
            dequeue_count=message.dequeue_count or 0,
            payload=payload if isinstance(payload, dict) else {},
        )

    def delete(self, message: QueueMessage) -> None:
        """Remove a processed message; does nothing when it no longer exists."""
        with suppress(ResourceNotFoundError):
            self._client.delete_message(message.id, message.pop_receipt)


def build_azure_storage(
    settings: Settings,
) -> tuple[AzureTableStore, AzureBlobStore, AzureJobQueue, AzureJobQueue]:
    """Connect to Azure Storage and create the table, container and queues if missing.

    A connection string takes precedence; otherwise the account name is used with the
    account key when set, or `DefaultAzureCredential` (e.g. managed identity) when not.

    Args:
        settings: Application settings with the account details and resource names.

    Returns:
        The table store, blob store, conversion queue and generation queue.

    Raises:
        RuntimeError: Neither a connection string nor an account name is configured.
    """
    if settings.azure_connection_string:
        tables = TableServiceClient.from_connection_string(settings.azure_connection_string)
        blobs = BlobServiceClient.from_connection_string(settings.azure_connection_string)
        queues = QueueServiceClient.from_connection_string(settings.azure_connection_string)
    elif settings.azure_account_name:
        account = settings.azure_account_name
        credential: Any
        if settings.azure_account_key:
            from azure.core.credentials import AzureNamedKeyCredential

            credential = AzureNamedKeyCredential(account, settings.azure_account_key)
        else:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        tables = TableServiceClient(
            f"https://{account}.table.core.windows.net", credential=credential
        )
        blobs = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential)
        queues = QueueServiceClient(
            f"https://{account}.queue.core.windows.net", credential=credential
        )
    else:
        raise RuntimeError(
            "Set AZURE_STORAGE_ACCOUNT_NAME (or AZURE_STORAGE_CONNECTION_STRING for an "
            "emulator) when HIA_STORAGE_BACKEND is 'azure'"
        )

    table_client = tables.create_table_if_not_exists(settings.table_name)
    container_client = blobs.get_container_client(settings.blob_container)
    queue_clients = [
        queues.get_queue_client(name) for name in (settings.convert_queue, settings.generate_queue)
    ]
    with suppress(ResourceExistsError):
        container_client.create_container()
    for queue_client in queue_clients:
        with suppress(ResourceExistsError):
            queue_client.create_queue()
    return (
        AzureTableStore(table_client),
        AzureBlobStore(container_client),
        AzureJobQueue(queue_clients[0]),
        AzureJobQueue(queue_clients[1]),
    )
