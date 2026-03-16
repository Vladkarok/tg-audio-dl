"""Live status message manager for the Telegram bot."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from enum import Enum

from telegram.error import BadRequest  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Step(Enum):
    RECEIVED = "received"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"


class StepStatus(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    ERROR = "error"


STEP_ICONS: dict[StepStatus, str] = {
    StepStatus.PENDING: "⬜",
    StepStatus.ACTIVE: "⏳",
    StepStatus.DONE: "✅",
    StepStatus.ERROR: "❌",
}

STEP_LABELS: dict[Step, str] = {
    Step.RECEIVED: "Link received",
    Step.DOWNLOADING: "Downloading audio",
    Step.PROCESSING: "Processing",
    Step.UPLOADING: "Uploading to Telegram",
}

_DEBOUNCE_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# ProgressManager
# ---------------------------------------------------------------------------


class ProgressManager:
    """Manages a live Telegram status message updated step-by-step."""

    def __init__(self, bot, chat_id: int, reply_to_message_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to_message_id = reply_to_message_id
        self._message_id: int | None = None
        self._last_edit_time: float = 0.0
        self._playlist_track: int | None = None
        self._playlist_total: int | None = None
        self._upload_animation_task: asyncio.Task | None = None

        # Initialise step state: RECEIVED=DONE, rest=PENDING
        self._steps: dict[Step, tuple[StepStatus, str]] = {
            Step.RECEIVED: (StepStatus.DONE, ""),
            Step.DOWNLOADING: (StepStatus.PENDING, ""),
            Step.PROCESSING: (StepStatus.PENDING, ""),
            Step.UPLOADING: (StepStatus.PENDING, ""),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(self) -> ProgressManager:
        """Send the initial status message. Returns self."""
        msg = await self._bot.send_message(
            chat_id=self._chat_id,
            text=self.render(),
            reply_to_message_id=self._reply_to_message_id,
        )
        self._message_id = msg.message_id
        return self

    async def set_step(
        self,
        step: Step,
        status: StepStatus,
        detail: str = "",
    ) -> None:
        """Update a step's status and optional detail. Debounced to 1 edit/sec."""
        if step == Step.UPLOADING and status in (StepStatus.DONE, StepStatus.ERROR):
            await self.stop_upload_animation()
        self._steps[step] = (status, detail)
        await self._maybe_edit()

    async def start_upload_animation(self) -> None:
        """Start cycling dots animation on the UPLOADING step."""
        if self._upload_animation_task is not None:
            return
        self._upload_animation_task = asyncio.create_task(self._animate_upload())

    async def stop_upload_animation(self) -> None:
        """Cancel the upload animation task."""
        if self._upload_animation_task is not None:
            self._upload_animation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._upload_animation_task
            self._upload_animation_task = None

    async def _animate_upload(self) -> None:
        """Cycle: '.' -> '..' -> '...' -> '.' every second."""
        dots_cycle = [".", "..", "..."]
        idx = 0
        try:
            while True:
                self._steps[Step.UPLOADING] = (StepStatus.ACTIVE, dots_cycle[idx % 3])
                await self._maybe_edit()
                await asyncio.sleep(1.0)
                idx += 1
        except asyncio.CancelledError:
            pass

    async def set_downloading_progress(self, percentage: float | None) -> None:
        """Update DOWNLOADING step with a percentage value. Debounced."""
        detail = f"{percentage:.0f}%" if percentage is not None else ""
        self._steps[Step.DOWNLOADING] = (StepStatus.ACTIVE, detail)
        await self._maybe_edit()

    async def set_playlist_context(self, track_index: int, total_tracks: int) -> None:
        """Set playlist header (Track X / Y)."""
        self._playlist_track = track_index
        self._playlist_total = total_tracks
        await self._maybe_edit()

    async def delete(self) -> None:
        """Delete the status message."""
        if self._message_id is not None:
            await self._bot.delete_message(
                chat_id=self._chat_id, message_id=self._message_id
            )

    def render(self) -> str:
        """Render current state to a message text string. Pure function, no I/O."""
        if self._playlist_track is not None and self._playlist_total is not None:
            header = (
                f"🎵 Playlist — Track {self._playlist_track} / {self._playlist_total}"
            )
        else:
            header = "🎵 Processing your request..."

        lines = [header, ""]
        for step in Step:
            status, detail = self._steps[step]
            icon = STEP_ICONS[status]
            label = STEP_LABELS[step]
            if detail:
                lines.append(f"{icon} {label}... {detail}")
            elif status == StepStatus.ACTIVE:
                lines.append(f"{icon} {label}...")
            else:
                lines.append(f"{icon} {label}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _maybe_edit(self) -> None:
        """Edit the message unless we are within the debounce window."""
        if self._message_id is None:
            return

        now = time.monotonic()
        if now - self._last_edit_time < _DEBOUNCE_SECONDS:
            return

        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=self.render(),
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("Failed to edit progress message: %s", exc)
            # Non-fatal: progress update failed but download continues

        self._last_edit_time = now
