from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Mapping


CAPTURE_SCHEDULE_MODE = "adaptive_balanced"
DEFAULT_BASE_INTERVAL_SECONDS = 15
UNCHANGED_INTERVAL_SECONDS = 30
RESOURCE_SAVING_INTERVAL_SECONDS = 60
RESOURCE_SAVING_STREAK = 5
RESOURCE_SAVING_IDLE_SECONDS = 120
CAPTURE_BACKOFF_REASONS = frozenset(
    {
        "baseline",
        "first_complete_display_set",
        "first_unchanged_repeat",
        "unchanged_repeat",
        "unchanged_idle",
        "unchanged_battery",
        "unchanged_low_power",
        "visual_change",
        "display_set_changed",
        "capture_error",
        "ambiguous_skip",
        "input_activity",
        "hid_unavailable",
        "session_locked",
        "session_unknown",
        "session_resumed",
        "system_resumed",
        "paused",
    }
)


@dataclass(frozen=True)
class AdaptiveCaptureState:
    """Content-free state for the adaptive keyframe schedule."""

    display_ids: frozenset[str]
    unchanged_streak: int
    effective_interval_seconds: int
    reason: str


@dataclass(frozen=True)
class CaptureObservation:
    """A content-free summary of one complete ScreenCaptureKit batch."""

    display_ids: frozenset[str]
    complete: bool
    all_unchanged: bool
    visual_change: bool
    capture_error: bool
    ambiguous_skip: bool


def initial_capture_state(
    base_interval_seconds: int = DEFAULT_BASE_INTERVAL_SECONDS,
) -> AdaptiveCaptureState:
    return AdaptiveCaptureState(
        display_ids=frozenset(),
        unchanged_streak=0,
        effective_interval_seconds=_base_interval(base_interval_seconds),
        reason="baseline",
    )


def reset_capture_state(
    state: AdaptiveCaptureState,
    *,
    reason: str,
    base_interval_seconds: int = DEFAULT_BASE_INTERVAL_SECONDS,
    clear_display_set: bool = False,
) -> AdaptiveCaptureState:
    return AdaptiveCaptureState(
        display_ids=frozenset() if clear_display_set else state.display_ids,
        unchanged_streak=0,
        effective_interval_seconds=_base_interval(base_interval_seconds),
        reason=str(reason or "baseline"),
    )


def capture_observation(
    events: Iterable[Mapping[str, object]],
    skipped: Iterable[Mapping[str, object]],
    errors: Iterable[Mapping[str, object]],
) -> CaptureObservation:
    """Summarize a batch without retaining OCR, titles, URLs, or frame hashes."""

    raw_events = tuple(events)
    raw_skipped = tuple(skipped)
    raw_errors = tuple(errors)
    event_items = tuple(item for item in raw_events if isinstance(item, Mapping))
    skipped_items = tuple(item for item in raw_skipped if isinstance(item, Mapping))
    error_items = tuple(item for item in raw_errors if isinstance(item, Mapping))
    all_items = (*event_items, *skipped_items, *error_items)
    all_items_well_formed = (
        len(event_items) == len(raw_events)
        and len(skipped_items) == len(raw_skipped)
        and len(error_items) == len(raw_errors)
    )
    display_values = tuple(str(item.get("display_id") or "").strip() for item in all_items)
    display_ids = frozenset(_display_key(value) for value in display_values if value)
    complete = bool(
        all_items
        and all_items_well_formed
        and all(display_values)
        and len(set(display_values)) == len(all_items)
    )
    skip_reasons = tuple(str(item.get("reason") or "").strip() for item in skipped_items)
    ambiguous_skip = not all_items_well_formed or any(reason != "duplicate_frame" for reason in skip_reasons)
    capture_error = bool(raw_errors)
    visual_change = bool(raw_events)
    all_unchanged = bool(
        complete
        and skipped_items
        and len(skipped_items) == len(all_items)
        and not capture_error
        and not visual_change
        and not ambiguous_skip
    )
    return CaptureObservation(
        display_ids=display_ids,
        complete=complete,
        all_unchanged=all_unchanged,
        visual_change=visual_change,
        capture_error=capture_error,
        ambiguous_skip=ambiguous_skip,
    )


def observation_payload(observation: CaptureObservation) -> dict[str, object]:
    return {
        "display_ids": sorted(observation.display_ids),
        "complete": observation.complete,
        "all_unchanged": observation.all_unchanged,
        "visual_change": observation.visual_change,
        "capture_error": observation.capture_error,
        "ambiguous_skip": observation.ambiguous_skip,
    }


def observation_from_payload(payload: object) -> CaptureObservation | None:
    if not isinstance(payload, Mapping):
        return None
    raw_display_ids = payload.get("display_ids")
    if not isinstance(raw_display_ids, (list, tuple, set, frozenset)):
        return None
    display_ids = frozenset(
        value
        for item in raw_display_ids
        if (value := str(item or "").strip())
    )
    return CaptureObservation(
        display_ids=display_ids,
        complete=payload.get("complete") is True,
        all_unchanged=payload.get("all_unchanged") is True,
        visual_change=payload.get("visual_change") is True,
        capture_error=payload.get("capture_error") is True,
        ambiguous_skip=payload.get("ambiguous_skip") is True,
    )


def advance_capture_state(
    state: AdaptiveCaptureState,
    observation: CaptureObservation,
    *,
    base_interval_seconds: int = DEFAULT_BASE_INTERVAL_SECONDS,
    idle_seconds: int | None = None,
    on_battery: bool = False,
    low_power_mode: bool = False,
) -> AdaptiveCaptureState:
    """Return the next schedule state for a single capture observation."""

    base_interval = _base_interval(base_interval_seconds)
    if observation.capture_error:
        return reset_capture_state(
            state,
            reason="capture_error",
            base_interval_seconds=base_interval,
            clear_display_set=True,
        )
    if observation.ambiguous_skip or not observation.complete:
        return reset_capture_state(
            state,
            reason="ambiguous_skip",
            base_interval_seconds=base_interval,
            clear_display_set=True,
        )
    if not state.display_ids:
        return AdaptiveCaptureState(
            display_ids=observation.display_ids,
            unchanged_streak=0,
            effective_interval_seconds=base_interval,
            reason="first_complete_display_set",
        )
    if observation.display_ids != state.display_ids:
        return AdaptiveCaptureState(
            display_ids=observation.display_ids,
            unchanged_streak=0,
            effective_interval_seconds=base_interval,
            reason="display_set_changed",
        )
    if observation.visual_change:
        return AdaptiveCaptureState(
            display_ids=observation.display_ids,
            unchanged_streak=0,
            effective_interval_seconds=base_interval,
            reason="visual_change",
        )
    if not observation.all_unchanged:
        return reset_capture_state(
            state,
            reason="ambiguous_skip",
            base_interval_seconds=base_interval,
            clear_display_set=True,
        )

    streak = state.unchanged_streak + 1
    if streak >= RESOURCE_SAVING_STREAK and (
        _idle_qualifies(idle_seconds) or on_battery or low_power_mode
    ):
        if low_power_mode:
            reason = "unchanged_low_power"
        elif on_battery:
            reason = "unchanged_battery"
        else:
            reason = "unchanged_idle"
        interval = max(base_interval, RESOURCE_SAVING_INTERVAL_SECONDS)
    elif streak >= 2:
        reason = "unchanged_repeat"
        interval = max(base_interval, UNCHANGED_INTERVAL_SECONDS)
    else:
        reason = "first_unchanged_repeat"
        interval = base_interval
    return AdaptiveCaptureState(
        display_ids=state.display_ids,
        unchanged_streak=streak,
        effective_interval_seconds=interval,
        reason=reason,
    )


def hid_input_detected(
    previous_idle_seconds: object,
    current_idle_seconds: object,
    *,
    elapsed_seconds: object | None = None,
) -> bool:
    """Detect a reset or an implausibly slow increase in the HID idle counter."""

    previous = _nonnegative_int_or_none(previous_idle_seconds)
    current = _nonnegative_int_or_none(current_idle_seconds)
    if previous is None or current is None:
        return False
    if current < previous:
        return True
    try:
        elapsed = max(0.0, float(elapsed_seconds))
    except (TypeError, ValueError):
        return False
    # The IOKit counter is integer-valued; allow 1.5 seconds for sampling and
    # truncation before treating missing growth as a reset caused by input.
    return current + 1.5 < previous + elapsed


def _idle_qualifies(idle_seconds: int | None) -> bool:
    value = _nonnegative_int_or_none(idle_seconds)
    return value is not None and value >= RESOURCE_SAVING_IDLE_SECONDS


def _base_interval(value: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_BASE_INTERVAL_SECONDS


def _nonnegative_int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _display_key(display_id: str) -> str:
    return hashlib.sha256(display_id.encode("utf-8", errors="replace")).hexdigest()
