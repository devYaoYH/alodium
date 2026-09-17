#!/usr/bin/env python3
"""
Offline tests for the log-stream filters in apps/floor/app.py. No server, no
docker, no network: these exercise the pure functions that decide what reaches
the panel, against the byte patterns a real agent-dev task container emits.

The regression they exist for: the spinner filter used to test `line[0]`
against a spinner rune, while the segments it was handed almost always OPENED
with an escape sequence (`\\x1b[2K...`). It had never matched anything in
production — a captured 337 KB stream came through at exactly 337 KB.

Run:  python3 tests/stream.py
Stdlib only.
"""
import importlib.util
import pathlib
import sys
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent / "app.py"
_spec = importlib.util.spec_from_file_location("floor_app", APP)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

ESC = "\x1b"
# One frame of forge's status line, verbatim in shape: CR home, erase line,
# a Braille spinner rune, then the coloured text.
SPIN = (f"{ESC}[2K{ESC}[32m⠦{ESC}[0m {ESC}[1;32mSynthesizing{ESC}[0m "
        f"{ESC}[37m3:40m{ESC}[0m {ESC}[2;37m· Ctrl+C to interrupt{ESC}[0m\r")


class Segments(unittest.TestCase):
    def test_round_trips_exactly(self):
        """Segmentation must be lossless — the client gets the container's bytes."""
        for text in ("a\r\nb\rc\nd",
                     "no terminator at all",
                     "\r\n\r\n",
                     "",
                     "trailing\r"):
            self.assertEqual("".join(app._segments(text)), text, repr(text))

    def test_splits_after_each_terminator(self):
        self.assertEqual(list(app._segments("a\r\nb\rc\nd")),
                         ["a\r\n", "b\r", "c\n", "d"])

    def test_keeps_crlf_together(self):
        self.assertEqual(list(app._segments("x\r\n")), ["x\r\n"])

    def test_does_not_split_on_vertical_tab_or_form_feed(self):
        """str.splitlines() breaks on these; in a log they are content."""
        for odd in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            self.assertEqual(list(app._segments(f"a{odd}b\n")), [f"a{odd}b\n"], repr(odd))


class SpinnerDetection(unittest.TestCase):
    def test_sees_through_leading_escapes(self):
        self.assertTrue(app._is_spinner(SPIN))

    def test_plain_spinner_still_detected(self):
        self.assertTrue(app._is_spinner("⠋ working\r"))
        self.assertTrue(app._is_spinner("|\r"))

    def test_ordinary_output_is_not_a_spinner(self):
        self.assertFalse(app._is_spinner(f"{ESC}[2KThe issue might be whitespace.\r\n"))
        self.assertFalse(app._is_spinner("\r\n"))
        self.assertFalse(app._is_spinner(f"{ESC}[0m\r\n"))

    def test_a_dash_bullet_is_not_mistaken_for_content(self):
        """'-' is in the ASCII spinner set; that is a known, accepted loss."""
        self.assertTrue(app._is_spinner("- a list item\r"))


class Filter(unittest.TestCase):
    def test_repeated_spinner_frames_collapse(self):
        keep, state = app._filter(SPIN, "")
        self.assertTrue(keep)
        keep, state = app._filter(SPIN, state)
        self.assertFalse(keep, "an identical repaint is bandwidth, not information")

    def test_spinner_reemits_when_its_text_changes(self):
        _, state = app._filter(SPIN, "")
        keep, _ = app._filter(SPIN.replace("3:40m", "3:41m"), state)
        self.assertTrue(keep)

    def test_content_always_survives(self):
        for seg in (f"{ESC}[2KThe issue might be whitespace.\r\n",
                    "  indented continuation\n",
                    "\n"):
            keep, _ = app._filter(seg, "irrelevant")
            self.assertTrue(keep, repr(seg))

    def test_content_resets_the_dedup_state(self):
        _, state = app._filter(SPIN, "")
        _, state = app._filter("real output\n", state)
        keep, _ = app._filter(SPIN, state)
        self.assertTrue(keep, "a spinner after real output is a fresh one")

    def test_a_short_hex_line_is_content_like_any_other(self):
        """These used to be dropped as "forge tool-call IDs"; they were this
        server's own HTTP chunk-size prefixes leaking into its own body."""
        for seg in ("d0\n", "ff\n", "5a\n", "3661\n"):
            keep, _ = app._filter(seg, "")
            self.assertTrue(keep, repr(seg))

    def test_a_real_stream_shrinks(self):
        """End to end on a synthetic stream shaped like a real task container."""
        stream = (SPIN * 40) + f"{ESC}[2Kfirst real line\r\n" + (SPIN * 40) \
            + f"{ESC}[2Ksecond real line\r\n"
        out, state = [], ""
        for seg in app._segments(stream):
            keep, state = app._filter(seg, state)
            if keep:
                out.append(seg)
        kept = "".join(out)
        self.assertIn("first real line", kept)
        self.assertIn("second real line", kept)
        self.assertEqual(kept.count("Synthesizing"), 2, "one frame per run of repaints")
        self.assertLess(len(kept), len(stream) / 4)


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[sys.argv[0]])
