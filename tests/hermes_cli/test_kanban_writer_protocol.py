import io
import pytest
from hermes_cli import kanban_writer_protocol as proto


def test_roundtrip_request():
    req = proto.Request(req_id="r1", op="create_task", kwargs={"title": "x", "assignee": "engineer"})
    blob = proto.encode(req.to_wire())
    buf = io.BytesIO(blob)
    got = proto.read_frame(buf.read)
    assert got == req.to_wire()


def test_partial_frame_raises_eof():
    truncated = (10).to_bytes(4, "big") + b"abc"
    buf = io.BytesIO(truncated)
    with pytest.raises(proto.FrameError):
        proto.read_frame(buf.read)


def test_oversize_frame_rejected():
    too_big = (proto.MAX_FRAME_BYTES + 1).to_bytes(4, "big")
    buf = io.BytesIO(too_big)
    with pytest.raises(proto.FrameError):
        proto.read_frame(buf.read)
