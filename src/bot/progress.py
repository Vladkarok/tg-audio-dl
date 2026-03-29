"""Live status message manager for the Telegram bot."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from enum import Enum

from telegram import Bot
from telegram.error import BadRequest

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


_STATUS_ICONS: dict[StepStatus, str] = {
    StepStatus.PENDING: "⏳",
    StepStatus.ACTIVE: "⏳",
    StepStatus.DONE: "✅",
    StepStatus.ERROR: "❌",
}

_STEP_ACTIVE_ICONS: dict[Step, str] = {
    Step.RECEIVED: "⏳",
    Step.DOWNLOADING: "⬇️",
    Step.PROCESSING: "🔄",
    Step.UPLOADING: "⬆️",
}

STEP_LABELS: dict[Step, str] = {
    Step.RECEIVED: "Link received",
    Step.DOWNLOADING: "Downloading audio",
    Step.PROCESSING: "Processing",
    Step.UPLOADING: "Uploading to Telegram",
}


def step_icon(step: Step, status: StepStatus) -> str:
    """Return the icon for a step+status combination."""
    if status == StepStatus.ACTIVE:
        return _STEP_ACTIVE_ICONS[step]
    return _STATUS_ICONS[status]


_DEBOUNCE_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# ProgressManager
# ---------------------------------------------------------------------------


class ProgressManager:
    """Manages a live Telegram status message updated step-by-step."""

    def __init__(self, bot: Bot, chat_id: int, reply_to_message_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to_message_id = reply_to_message_id
        self._message_id: int | None = None
        self._last_edit_time: float = 0.0
        self._playlist_track: int | None = None
        self._playlist_total: int | None = None
        self._playlist_track_title: str | None = None
        self._animation_tasks: dict[Step, asyncio.Task[None]] = {}
        # Task that flushes a pending edit after the debounce window expires.
        # Ensures the last state update is always sent even if it arrives while
        # the debounce window is active.
        self._deferred_flush_task: asyncio.Task[None] | None = None

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
        if status in (StepStatus.DONE, StepStatus.ERROR):
            await self._stop_animation(step)
        self._steps[step] = (status, detail)
        await self._maybe_edit()

    async def start_animation(self, step: Step) -> None:
        """Start cycling dots animation on the given step."""
        if step in self._animation_tasks:
            return
        self._animation_tasks[step] = asyncio.create_task(self._animate(step))

    async def start_upload_animation(self) -> None:
        """Start cycling dots animation on the UPLOADING step."""
        await self.start_animation(Step.UPLOADING)

    async def _stop_animation(self, step: Step) -> None:
        """Cancel animation task for a step if running."""
        task = self._animation_tasks.pop(step, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop_upload_animation(self) -> None:
        """Cancel the upload animation task."""
        await self._stop_animation(Step.UPLOADING)

    async def _animate(self, step: Step) -> None:
        """Cycle: '.' -> '..' -> '...' -> '.' every second."""
        dots_cycle = [".", "..", "..."]
        idx = 0
        try:
            while True:
                self._steps[step] = (StepStatus.ACTIVE, dots_cycle[idx % 3])
                await self._maybe_edit()
                # Use a real event-loop future so the loop yields even when
                # asyncio.sleep is mocked in tests.
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[None] = loop.create_future()
                loop.call_later(
                    1.0,
                    lambda f=fut: f.set_result(None) if not f.done() else None,  # type: ignore[misc]
                )
                await fut
                idx += 1
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Animation error for step %s", step, exc_info=True)

    async def set_downloading_progress(self, percentage: float | None) -> None:
        """Update DOWNLOADING step with a percentage value. Debounced."""
        detail = f"{percentage:.0f}%" if percentage is not None else ""
        self._steps[Step.DOWNLOADING] = (StepStatus.ACTIVE, detail)
        await self._maybe_edit()

    async def set_playlist_context(
        self,
        track_index: int,
        total_tracks: int,
        track_title: str | None = None,
    ) -> None:
        """Set playlist header (Track X / Y) with optional track title."""
        self._playlist_track = track_index
        self._playlist_total = total_tracks
        self._playlist_track_title = track_title
        await self._maybe_edit()

    async def edit_text(self, text: str) -> None:
        """Replace the progress message content with arbitrary *text*.

        Cancels any pending deferred flush so it cannot overwrite this
        explicit text (e.g. an error message set via _edit_error).
        """
        if self._deferred_flush_task is not None:
            self._deferred_flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._deferred_flush_task
            self._deferred_flush_task = None
        if self._message_id is not None:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
            )

    async def delete(self) -> None:
        """Stop all animations and delete the status message."""
        # Cancel any pending deferred flush — no point flushing a message
        # that is about to be deleted.
        if self._deferred_flush_task is not None:
            self._deferred_flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._deferred_flush_task
            self._deferred_flush_task = None
        for step in list(self._animation_tasks):
            await self._stop_animation(step)
        if self._message_id is not None:
            with contextlib.suppress(Exception):
                await self._bot.delete_message(
                    chat_id=self._chat_id, message_id=self._message_id
                )
            self._message_id = None

    def render(self) -> str:
        """Render current state to a message text string. Pure function, no I/O."""
        if self._playlist_track is not None and self._playlist_total is not None:
            header = (
                f"🎵 Playlist — Track {self._playlist_track} / {self._playlist_total}"
            )
            if self._playlist_track_title:
                title = self._playlist_track_title[:80]
                header = f"{header}\n📄 {title}"
        else:
            header = "🎵 Processing your request..."

        lines = [header, ""]
        for step in Step:
            status, detail = self._steps[step]
            icon = step_icon(step, status)
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
        """Edit the message unless we are within the debounce window.

        If the window is active, schedule a deferred flush so that the
        latest state is always sent — even when no further update arrives.
        """
        if self._message_id is None:
            return

        now = time.monotonic()
        elapsed = now - self._last_edit_time
        if elapsed < _DEBOUNCE_SECONDS:
            # Schedule a deferred flush only if one is not already pending.
            # Sleep only for the remaining time so the flush happens at the
            # end of the current window, not a full debounce period later.
            remaining = _DEBOUNCE_SECONDS - elapsed
            if self._deferred_flush_task is None or self._deferred_flush_task.done():
                self._deferred_flush_task = asyncio.create_task(
                    self._deferred_flush(remaining)
                )
            return

        await self._flush_edit()

    async def _deferred_flush(self, delay: float) -> None:
        """Wait *delay* seconds then flush the message."""
        try:
            await asyncio.sleep(delay)
            self._deferred_flush_task = None
            await self._flush_edit()
        except asyncio.CancelledError:
            pass

    async def _flush_edit(self) -> None:
        """Perform the actual edit_message_text call."""
        if self._message_id is None:
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
            return
        self._last_edit_time = time.monotonic()
