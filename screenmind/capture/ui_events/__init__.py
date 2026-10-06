"""
UI event capture: clicks, typed text, app switches and clipboard changes,
enriched with accessibility info about the element the user interacted with.

See docs/plans/ui-events.md.
"""

from screenmind.capture.ui_events.models import EventType, UiEvent, describe_event
from screenmind.capture.ui_events.recorder import UiEventRecorder, create_backend

__all__ = ["EventType", "UiEvent", "UiEventRecorder", "create_backend", "describe_event"]
