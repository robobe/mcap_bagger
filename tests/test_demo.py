import runpy

import pytest


def test_generated_frames_contain_visible_changing_numbers():
    pytest.importorskip("PIL")
    make_frame = runpy.run_path("examples/publish_demo.py")["make_frame"]
    first = make_frame(0)
    second = make_frame(42)
    assert len(first) == len(second) == 640 * 480 * 3
    assert first != second
    assert b"\xff\xff\xff" in first and b"\xff\xff\xff" in second
