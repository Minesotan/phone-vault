"""Exception types shared across the package."""


class PhonevaultError(Exception):
    """Base class for every error this program raises deliberately."""


class RepoError(PhonevaultError):
    """The repository is missing, malformed, or an incompatible version."""


class LockError(RepoError):
    """Another process holds the repository lock."""


class BackendError(PhonevaultError):
    """The storage backend could not complete an operation."""


class SourceError(PhonevaultError):
    """The backup source (adb device, local filesystem) failed."""


class AdbError(SourceError):
    """An adb invocation failed or the device is not in a usable state."""


class BatchReadError(SourceError):
    """The batched (tar) reader failed partway through a group of files.

    Distinct from both a lost device and one unreadable file: the transport
    is fine and the files are fine, but this particular tar stream can no
    longer be trusted. The remaining files have to be re-read one at a time.
    """


class DeviceLostError(AdbError):
    """The device went away mid-operation.

    Kept distinct from a plain :class:`AdbError` because the two call for
    opposite responses: one unreadable file should be skipped and the backup
    continued, whereas a vanished device means every later read would fail
    too, and the right move is to wait for it and resume.
    """


class RestoreError(PhonevaultError):
    """A restore could not be completed."""


class IntegrityError(RepoError):
    """Stored data did not match its recorded hash."""


class BackupPaused(PhonevaultError):
    """A backup stopped because it was asked to, not because it broke.

    Carries the statistics gathered so far so the caller can report honest
    numbers. Everything read up to this point is stored and recorded in a
    checkpoint, so re-running the backup continues from here.
    """

    def __init__(self, message: str, stats: dict | None = None) -> None:
        super().__init__(message)
        self.stats = stats or {}
