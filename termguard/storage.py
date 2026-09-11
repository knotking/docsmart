"""Layer S (substrate): content-addressed object storage.

Every byte of every document version passes through an :class:`ObjectStore`. Blobs are
addressed by the SHA-256 of their content, so:

* identical content is stored exactly once,
* the address *is* an integrity proof - a version row naming ``sha256:abc...`` can be
  checked against the bytes at any later date,
* nothing in the pipeline needs to know whether it is talking to a local directory or a
  GCS bucket.

Constraint 8 in CLAUDE.md: no pipeline module opens a document path directly.

Local layout::

    data/blobs/sha256/ab/cd/abcd1234....docx

GCS layout (same key, bucket-relative)::

    gs://<bucket>/<prefix>/sha256/ab/cd/abcd1234....docx
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from termguard.config import Settings, get_settings

_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so large documents stay cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def blob_key(digest: str, suffix: str = ".docx") -> str:
    """Storage key for a digest. Sharded two levels so a directory never grows unbounded."""
    if len(digest) < 4:
        raise ValueError(f"implausible digest: {digest!r}")
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"


class ObjectStore(ABC):
    """A content-addressed blob store."""

    @abstractmethod
    def put_bytes(self, data: bytes, *, suffix: str = ".docx") -> str:
        """Store ``data``; return its hex digest. Idempotent."""

    @abstractmethod
    def get_bytes(self, digest: str, *, suffix: str = ".docx") -> bytes:
        """Read a blob back by digest. Raises :class:`KeyError` if absent."""

    @abstractmethod
    def exists(self, digest: str, *, suffix: str = ".docx") -> bool:
        """True if a blob with this digest is present."""

    @abstractmethod
    def uri(self, digest: str, *, suffix: str = ".docx") -> str:
        """A stable, backend-qualified locator recorded in the audit trail."""

    # -- shared conveniences -------------------------------------------------

    def put_file(self, path: Path, *, suffix: str | None = None) -> str:
        """Store a file's contents; return its digest."""
        path = Path(path)
        return self.put_bytes(path.read_bytes(), suffix=suffix or path.suffix or ".bin")

    def get_file(self, digest: str, dest: Path, *, suffix: str = ".docx") -> Path:
        """Materialize a blob at ``dest``. Returns ``dest``."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.get_bytes(digest, suffix=suffix))
        return dest

    @contextmanager
    def temp_copy(self, digest: str, *, suffix: str = ".docx", name: str | None = None) -> Iterator[Path]:
        """Yield a temporary local path holding the blob's bytes.

        The bridge for libraries that insist on a real filesystem path (python-docx,
        docx-editor). The copy is deleted on exit, so no pipeline step can accidentally
        mutate stored content.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="termguard-"))
        try:
            target = tmpdir / (name or f"{digest[:12]}{suffix}")
            self.get_file(digest, target, suffix=suffix)
            yield target
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def verify(self, digest: str, *, suffix: str = ".docx") -> bool:
        """Re-hash stored bytes and confirm they still match the digest they are filed under."""
        try:
            return sha256_bytes(self.get_bytes(digest, suffix=suffix)) == digest
        except KeyError:
            return False


class LocalObjectStore(ObjectStore):
    """Filesystem backend. The default for local runs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str, suffix: str) -> Path:
        return self.root / blob_key(digest, suffix)

    def put_bytes(self, data: bytes, *, suffix: str = ".docx") -> str:
        digest = sha256_bytes(data)
        path = self._path(digest, suffix)
        if path.exists():
            return digest  # content-addressed: already stored, byte-identical by definition
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a reader never sees a half-written blob.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest

    def get_bytes(self, digest: str, *, suffix: str = ".docx") -> bytes:
        path = self._path(digest, suffix)
        if not path.exists():
            raise KeyError(f"no blob {digest} in {self.root}")
        return path.read_bytes()

    def exists(self, digest: str, *, suffix: str = ".docx") -> bool:
        return self._path(digest, suffix).exists()

    def uri(self, digest: str, *, suffix: str = ".docx") -> str:
        return self._path(digest, suffix).as_uri()


class GCSObjectStore(ObjectStore):
    """Google Cloud Storage backend.

    Imported lazily so the local demo never needs ``google-cloud-storage`` installed.
    Install with ``pip install -e ".[gcp]"`` and set ``TERMGUARD_STORAGE=gcs`` plus
    ``TERMGUARD_GCS_BUCKET``. Authentication is Application Default Credentials, so on
    Cloud Run the attached service account is used with no key material in the image.
    """

    def __init__(self, bucket: str, prefix: str = "termguard") -> None:
        if not bucket:
            raise ValueError("TERMGUARD_GCS_BUCKET must be set when TERMGUARD_STORAGE=gcs")
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only in GCP deployments
            raise RuntimeError(
                'GCS backend requires the gcp extra: pip install -e ".[gcp]"'
            ) from exc
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")

    def _key(self, digest: str, suffix: str) -> str:
        key = blob_key(digest, suffix)
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, data: bytes, *, suffix: str = ".docx") -> str:  # pragma: no cover
        digest = sha256_bytes(data)
        blob = self._bucket.blob(self._key(digest, suffix))
        if blob.exists():
            return digest
        # if_generation_match=0 makes concurrent writers safe: whoever loses the race gets
        # a PreconditionFailed, and the content is identical anyway.
        try:
            blob.upload_from_string(data, if_generation_match=0)
        except Exception:
            if not blob.exists():
                raise
        return digest

    def get_bytes(self, digest: str, *, suffix: str = ".docx") -> bytes:  # pragma: no cover
        blob = self._bucket.blob(self._key(digest, suffix))
        if not blob.exists():
            raise KeyError(f"no blob {digest} in gs://{self.bucket_name}")
        return blob.download_as_bytes()

    def exists(self, digest: str, *, suffix: str = ".docx") -> bool:  # pragma: no cover
        return self._bucket.blob(self._key(digest, suffix)).exists()

    def uri(self, digest: str, *, suffix: str = ".docx") -> str:  # pragma: no cover
        return f"gs://{self.bucket_name}/{self._key(digest, suffix)}"


def get_store(settings: Settings | None = None) -> ObjectStore:
    """Build the configured store. The only place a backend is chosen."""
    settings = settings or get_settings()
    if settings.storage_backend == "gcs":
        return GCSObjectStore(settings.gcs_bucket, settings.gcs_prefix)
    if settings.storage_backend == "local":
        return LocalObjectStore(settings.blob_root)
    raise ValueError(f"unknown storage backend: {settings.storage_backend!r}")
