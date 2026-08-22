"""Tests for src/bot/timestamps.py — the hand-entered timestamp parser."""

from __future__ import annotations

import pytest

from src.bot.timestamps import (
    MAX_CHAPTERS,
    MAX_INPUT_CHARS,
    MAX_TITLE_CHARS,
    TimestampParseError,
    parse_timestamps,
)


class TestAcceptedForms:
    def test_leading_timestamps(self):
        assert parse_timestamps("0:00 Intro\n3:45 Second\n1:02:10 Closer") == (
            (0, "Intro"),
            (225, "Second"),
            (3730, "Closer"),
        )

    @pytest.mark.parametrize(
        "line",
        [
            "0:00 Intro",
            "00:00 Intro",
            "0:00 - Intro",
            "0:00 – Intro",
            "0:00 — Intro",
            "0:00 | Intro",
            "[0:00] Intro",
            "(0:00) Intro",
            "- 0:00 Intro",
            "• 0:00 Intro",
            "0:00. Intro",
        ],
    )
    def test_decoration_variants(self, line):
        assert parse_timestamps(line) == ((0, "Intro"),)

    def test_trailing_timestamps(self):
        """Comments often put the time at the end of the line."""
        assert parse_timestamps("Intro 0:00\nSecond - 3:45") == (
            (0, "Intro"),
            (225, "Second"),
        )

    def test_hours_roll_over(self):
        assert parse_timestamps("2:03:04 Late")[0][0] == 2 * 3600 + 3 * 60 + 4

    def test_bare_minutes_may_exceed_59(self):
        """M:SS with no hour part is a common way to write 90 minutes in."""
        assert parse_timestamps("90:00 Track")[0][0] == 5400

    def test_blank_lines_skipped(self):
        assert len(parse_timestamps("0:00 A\n\n  \n3:00 B")) == 2

    def test_titles_keep_internal_punctuation(self):
        assert parse_timestamps("0:00 A.R.T. — live (2019)") == (
            (0, "A.R.T. — live (2019)"),
        )


class TestRejections:
    def test_no_timestamp(self):
        with pytest.raises(TimestampParseError, match="line 1: no timestamp found"):
            parse_timestamps("just some text")

    def test_empty_input(self):
        with pytest.raises(TimestampParseError, match="No timestamps found"):
            parse_timestamps("   \n\n")

    def test_missing_title(self):
        with pytest.raises(TimestampParseError, match="no title"):
            parse_timestamps("0:00")

    def test_seconds_out_of_range(self):
        with pytest.raises(TimestampParseError, match="seconds must be 00-59"):
            parse_timestamps("0:75 Nope")

    def test_minutes_out_of_range_with_hours(self):
        with pytest.raises(TimestampParseError, match="minutes must be 00-59"):
            parse_timestamps("1:75:00 Nope")

    def test_non_increasing_is_rejected_not_sorted(self):
        """Silently sorting would hide a typo or two pasted tracklists."""
        with pytest.raises(TimestampParseError, match="is not after"):
            parse_timestamps("5:00 Later\n1:00 Earlier")

    def test_duplicate_start_rejected(self):
        with pytest.raises(TimestampParseError, match="is not after"):
            parse_timestamps("0:00 A\n0:00 B")

    def test_ambiguous_two_timestamps(self):
        with pytest.raises(TimestampParseError, match="two timestamps"):
            parse_timestamps("1:00 sampled at 2:00")

    def test_past_duration(self):
        with pytest.raises(TimestampParseError, match="past the end"):
            parse_timestamps("0:00 A\n20:00 B", duration_seconds=600)

    def test_duration_boundary_is_inclusive(self):
        assert parse_timestamps("10:00 End", duration_seconds=600) == ((600, "End"),)

    def test_unknown_duration_skips_the_bound(self):
        assert parse_timestamps("99:00 A", duration_seconds=None)[0][0] == 5940

    def test_errors_are_line_numbered_and_capped(self):
        text = "\n".join(f"bad line {i}" for i in range(20))
        with pytest.raises(TimestampParseError) as exc:
            parse_timestamps(text)
        message = str(exc.value)
        assert "line 1:" in message
        assert "and 15 more" in message

    def test_title_too_long(self):
        with pytest.raises(TimestampParseError, match="title longer than"):
            parse_timestamps(f"0:00 {'x' * (MAX_TITLE_CHARS + 1)}")

    def test_too_many_chapters(self):
        text = "\n".join(f"{i}:0{i % 10} Track {i}" for i in range(MAX_CHAPTERS + 1))
        with pytest.raises(TimestampParseError, match="Too many chapters"):
            parse_timestamps(text)

    def test_input_too_long(self):
        with pytest.raises(TimestampParseError, match="Input too long"):
            parse_timestamps("x" * (MAX_INPUT_CHARS + 1))


class TestSanitisation:
    def test_invisible_characters_are_stripped(self):
        """Bidi overrides and zero-width joiners can hide or reorder caption text."""
        parsed = parse_timestamps("0:00 In‮tro​")
        assert parsed == ((0, "Intro"),)

    def test_runs_of_whitespace_collapse(self):
        assert parse_timestamps("0:00 Two    words") == ((0, "Two words"),)
