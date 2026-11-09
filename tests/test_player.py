import io

import pytest
from PIL import Image
from mcap.writer import Writer

from mcap_bagger.player import Recording, decode_image, payload_text


def test_image_decode():
    pixels = bytes([255, 0, 0]) * 4
    info = dict(width=2, height=2, step=6, encoding='rgb8')
    assert decode_image(pixels, 'raw', info).getpixel((0, 0)) == (255, 0, 0)
    info['encoding'] = 'bgr8'
    assert decode_image(pixels, 'raw', info).getpixel((0, 0)) == (0, 0, 255)
    with pytest.raises(ValueError):
        decode_image(pixels, 'raw', None)
    with pytest.raises(ValueError):
        decode_image(b'', 'raw', info)
    buffer = io.BytesIO()
    Image.new('RGB', (640, 480)).save(buffer, 'JPEG')
    assert decode_image(buffer.getvalue(), 'jpeg', None).size == (640, 480)


def test_payload_rendering():
    assert '\n' in payload_text(b'{"x": 1}')
    assert payload_text(b'text') == 'text'
    assert payload_text(b'\x00\xff') == '00 ff'
    assert payload_text(b'x' * 20000).endswith('[truncated]')


def test_recording_order_reload_and_empty(tmp_path):
    path = tmp_path / 'recording.mcap'
    with path.open('wb') as stream:
        writer = Writer(stream)
        writer.start()
        channel = writer.register_channel('imu', '', 0)
        for stamp in (3, 1, 2):
            writer.add_message(channel, stamp, b'{}', stamp)
        writer.finish()
    recording = Recording()
    recording.load(path)
    old_stream = recording.stream
    for stamp in (1, 2, 3):
        assert recording.next_message[2].log_time == stamp
        recording.advance()
    assert recording.next_message is None
    recording.load(path)
    assert old_stream.closed and recording.next_message[2].log_time == 1
    recording.close()
    assert recording.stream is None
    with pytest.raises(ValueError):
        recording.load(tmp_path / 'bad.mcap.partial')
    assert recording.stream is None


def test_empty_and_invalid_recordings(tmp_path):
    path = tmp_path / 'empty.mcap'
    with path.open('wb') as stream:
        writer = Writer(stream)
        writer.start()
        writer.finish()
    recording = Recording()
    recording.load(path)
    assert recording.origin is None and recording.next_message is None
    recording.close()
    path.write_bytes(b'invalid')
    with pytest.raises(Exception):
        recording.load(path)
    assert recording.stream is None


def test_gui_controls_and_eof(tmp_path, monkeypatch):
    tk = pytest.importorskip('tkinter')
    from tkinter import filedialog
    from mcap_bagger.player import open_player
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip('desktop display unavailable')
    root.withdraw()
    path = tmp_path / 'short.mcap'
    buffer = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buffer, 'JPEG')
    with path.open('wb') as stream:
        writer = Writer(stream)
        writer.start()
        channel = writer.register_channel('image', '', 0, {'shm': 'true', 'compress': 'jpeg'})
        for stamp in (0, 1_000_000):
            writer.add_message(channel, stamp, buffer.getvalue(), stamp)
        writer.finish()
    monkeypatch.setattr(tk, 'Tk', lambda: root)
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **kwargs: str(path))
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)

    def buttons():
        return {w.cget('text'): w for frame in root.winfo_children()
                if frame.winfo_class() == 'TFrame' for w in frame.winfo_children()}

    def begin():
        buttons()['Load'].invoke()
        buttons()['Step'].invoke()

    def resume():
        assert any(getattr(w, 'image', None) is not None for w in root.winfo_children())
        buttons()['Start'].invoke()

    def restart():
        assert str(buttons()['Stop']['state']) == 'disabled'
        buttons()['Start'].invoke()
        buttons()['Stop'].invoke()
        buttons()['Load'].invoke()

    root.after(0, begin)
    root.after(30, resume)
    root.after(80, restart)
    root.after(120, lambda: root.tk.call(root.protocol('WM_DELETE_WINDOW')))
    open_player()
    assert not errors
