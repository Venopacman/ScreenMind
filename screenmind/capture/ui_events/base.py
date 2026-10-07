"""
Abstract interface for OS-specific UI event backends.

A backend owns the OS input hook and answers accessibility questions.
The hook runs on its own thread and only puts RawEvents on a queue; the
recorder's enricher thread calls everything else.
"""

import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from screenmind.capture.ui_events.models import ElementInfo


@dataclass
class PermissionStatus:
    input_monitoring: bool
    accessibility: bool

    @property
    def all_granted(self) -> bool:
        return self.input_monitoring and self.accessibility

    def as_dict(self) -> dict:
        return {
            "input_monitoring": self.input_monitoring,
            "accessibility": self.accessibility,
            "all_granted": self.all_granted,
        }


@dataclass
class FrontWindow:
    pid: Optional[int]
    app_name: Optional[str]
    title: Optional[str]


class UiEventBackend(ABC):
    name = "unknown"

    @abstractmethod
    def check_permissions(self) -> PermissionStatus:
        ...

    @abstractmethod
    def request_permissions(self) -> PermissionStatus:
        """Ask the OS to show its permission prompts. Returns the status after."""
        ...

    @abstractmethod
    def start(self, out: "queue.SimpleQueue") -> bool:
        """Install the input hook on a new thread. Returns False if it failed."""
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def is_running(self) -> bool:
        ...

    @abstractmethod
    def element_at(self, x: float, y: float) -> Optional[ElementInfo]:
        ...

    @abstractmethod
    def focused_element(self) -> Optional[ElementInfo]:
        ...

    @abstractmethod
    def front_window(self) -> Optional[FrontWindow]:
        ...

    def browser_url(self) -> Optional[str]:
        """Page URL of the frontmost browser window, or None."""
        try:
            from screenmind.platform_support import adapter
            return adapter().get_browser_url()
        except Exception:
            return None

    def tap_stats(self) -> dict:
        """Health of the input hook, read by the recorder from its own thread.
        Keys: keys_tapped (bool), callback_errors (int), last_callback_error
        (str or None), reenabled (int, times the OS turned the hook off)."""
        return {}

    def app_name_for_pid(self, pid: int) -> Optional[str]:
        """Name of the app that owns a process. Optional for backends."""
        return None

    @abstractmethod
    def clipboard_change_count(self) -> Optional[int]:
        ...

    @abstractmethod
    def read_clipboard(self) -> Optional[str]:
        """Return the clipboard text, or None if it is empty, not text, or
        marked as secret by a password manager."""
        ...
