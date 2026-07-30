from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import AvatarResult, JobRecord, ProgressUpdate


class GenerationCanceled(Exception):
    """Raised when a task was canceled while the provider was processing it."""


class AvatarAdapter(Protocol):
    @property
    def worker_id(self) -> str: ...

    def generate(
        self,
        job: JobRecord,
        report: Callable[[ProgressUpdate], None],
        should_cancel: Callable[[], bool],
    ) -> AvatarResult: ...
