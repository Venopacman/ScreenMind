"""
Pydantic Models for ScreenMind
Defines the data structures used across the application.
"""

from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


class ActivityRecord(BaseModel):
    """
    Structured output from Gemma 4 screenshot analysis.
    This is what the model returns after analyzing a screenshot.
    """

    app_name: str = Field(
        default="unknown",
        description="Primary application visible (e.g., VS Code, Chrome, Slack)",
    )
    activity_category: str = Field(
        default="other",
        description="One of: coding, browsing, communication, writing, design, media, terminal, meeting, idle, other",
    )
    activity_summary: str = Field(
        default="",
        description="One sentence describing what the user is doing",
    )
    detailed_context: str = Field(
        default="",
        description="2-3 sentences with specific details",
    )
    visible_text_snippets: List[str] = Field(
        default_factory=list,
        description="Key text visible on screen, max 5 items",
    )
    mood: str = Field(
        default="neutral",
        description="productive, distracted, collaborative, learning, or neutral",
    )
    confidence: float = Field(
        default=0.5,
        description="Model confidence 0.0 to 1.0",
        ge=0.0,
        le=1.0,
    )
    scene_description: str = Field(
        default="",
        description="Rich visual narration of the screenshot: layout, conversations, people, notifications, actionable items",
    )


class ScreenshotEntry(BaseModel):
    """
    Complete entry for a single captured & analyzed screenshot.
    Combines capture metadata and Gemma 4 analysis.
    """

    id: Optional[int] = None
    # TODO: migrate to timezone-aware UTC timestamps (datetime.now(timezone.utc)).
    # All 25+ call sites currently use naive local time. Switching partially would
    # create mixed timezones in the DB. Requires a full migration + display updates.
    timestamp: datetime
    screenshot_path: str
    window_title: Optional[str] = None
    detected_app_name: Optional[str] = None  # From OS-level window detection

    # Gemma 4 analysis results
    analysis: Optional[ActivityRecord] = None

    # Processing status
    analyzed: bool = False
    analysis_error: Optional[str] = None
