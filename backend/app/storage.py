import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    def save(self, path: str, data: bytes) -> None: ...

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None: ...


class LocalStorage(StorageBackend):
    """Stores files on local disk under a root directory. Used when no
    Azure Storage connection string is configured (e.g. local development)."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, path: str) -> Path:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def save(self, path: str, data: bytes) -> None:
        self._full_path(path).write_bytes(data)

    def read(self, path: str) -> bytes:
        return self._full_path(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._full_path(path).exists()

    def delete_prefix(self, prefix: str) -> None:
        target = self.root / prefix
        if target.exists():
            shutil.rmtree(target)


class AzureBlobStorage(StorageBackend):
    """Stores files in an Azure Blob Storage container. Subjects are
    represented as blob name prefixes (e.g. 'social/raw/notes.pdf'),
    since blob storage has no real directories."""

    def __init__(self, connection_string: str, container_name: str):
        from azure.storage.blob import BlobServiceClient

        self.client = BlobServiceClient.from_connection_string(connection_string)
        self.container_name = container_name
        try:
            self.client.create_container(container_name)
        except Exception:
            pass  # container already exists
        self.container = self.client.get_container_client(container_name)

    def save(self, path: str, data: bytes) -> None:
        self.container.upload_blob(name=path, data=data, overwrite=True)

    def read(self, path: str) -> bytes:
        return self.container.download_blob(path).readall()

    def exists(self, path: str) -> bool:
        return self.container.get_blob_client(path).exists()

    def delete_prefix(self, prefix: str) -> None:
        for blob in self.container.list_blobs(name_starts_with=prefix):
            self.container.delete_blob(blob.name)


_storage_instance: StorageBackend | None = None


def get_storage() -> StorageBackend:
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if connection_string:
        container = os.environ.get("AZURE_STORAGE_CONTAINER", "materials")
        _storage_instance = AzureBlobStorage(connection_string, container)
    else:
        data_dir = os.environ.get(
            "APP_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "data")
        )
        _storage_instance = LocalStorage(os.path.join(data_dir, "materials"))

    return _storage_instance
