"""Small desktop viewer for opaque MCAP recordings."""
import io
import json
import time
from pathlib import Path

from mcap.reader import make_reader
from PIL import Image

LIMIT = 16 * 1024


def payload_text(payload):
    sample = payload[:LIMIT]
    try:
        text = sample.decode('utf-8')
        if len(payload) <= LIMIT:
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
            text = sample.hex(' ')
    except UnicodeDecodeError:
        text = sample.hex(' ')
    return text[:LIMIT] + ('\n[truncated]' if len(payload) > LIMIT or len(text) > LIMIT else '')


def decode_image(payload, compression, info):
    if compression == 'jpeg':
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            return image.convert('RGB')
    if compression != 'raw' or not info:
        raise ValueError('Image layout unavailable: waiting for camera_info')
    width, height, step = (info.get(key) for key in ('width', 'height', 'step'))
    encoding = info.get('encoding')
    if (any(type(n) is not int or n <= 0 for n in (width, height, step))
            or encoding not in ('rgb8', 'bgr8') or step < width * 3
            or step * height != len(payload) or len(payload) > 64 * 1024 * 1024):
        raise ValueError('Invalid raw image layout')
    return Image.frombytes('RGB', (width, height), payload, 'raw', 'RGB' if encoding == 'rgb8' else 'BGR', step)


class Recording:
    def __init__(self):
        self.stream = None
        self.path = None
        self.next_message = None
        self.origin = None

    def load(self, path):
        self.close()
        path = Path(path)
        if path.name.endswith('.partial'):
            raise ValueError('Load a finalized .mcap file, not a partial recording')
        try:
            self.stream = path.open('rb')
            reader = make_reader(self.stream)
            if reader.get_summary() is None:
                raise ValueError('Recording has no finalized MCAP summary')
            self.messages = reader.iter_messages(log_time_order=True)
            self.path = path
            self.advance()
            self.origin = self.next_message[2].log_time if self.next_message else None
        except Exception:
            self.close()
            raise

    def advance(self):
        self.next_message = next(self.messages, None)

    def close(self):
        if self.stream:
            self.stream.close()
        self.stream = None
        self.path = None
        self.next_message = None
        self.origin = None


def open_player():
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
        from tkinter.scrolledtext import ScrolledText
        from PIL import ImageTk
    except ImportError as exc:
        raise RuntimeError('The desktop player requires Tkinter (Ubuntu: python3-tk); install it manually.') from exc

    root = tk.Tk()
    root.title('MCAP test player')
    root.geometry('900x850')
    recording = Recording()
    state = dict(callback=None, mode=None, anchor=0, anchor_ns=0, position=0, info=None, values={})
    bar = ttk.Frame(root, padding=10)
    bar.pack(fill='x')
    status = tk.StringVar(value='Load a finalized MCAP recording')
    preview = ttk.Label(root, text='No image', anchor='center')
    preview.pack(fill='both', expand=True, padx=10)
    ttk.Label(root, textvariable=status, wraplength=850).pack(fill='x', padx=10)
    text = ScrolledText(root, height=15, state='disabled', wrap='word')
    text.pack(fill='both', expand=True, padx=10, pady=10)

    def controls():
        loaded = recording.path is not None
        start_button['state'] = 'normal' if loaded and state['mode'] != 'play' else 'disabled'
        stop_button['state'] = 'normal' if state['mode'] else 'disabled'
        step_button['state'] = 'normal' if loaded else 'disabled'

    def pause():
        if state['mode'] == 'play':
            state['position'] = state['anchor_ns'] + int((time.monotonic() - state['anchor']) * 1e9)
        state['mode'] = None
        if state['callback'] is not None:
            root.after_cancel(state['callback'])
            state['callback'] = None
        controls()

    def load(path=None):
        pause()
        if path is None:
            path = filedialog.askopenfilename(filetypes=[('MCAP recordings', '*.mcap'), ('All files', '*')])
        if not path:
            return
        try:
            recording.load(path)
            state.update(position=recording.origin or 0, info=None, values={})
            preview.configure(image='', text='No image')
            preview.image = None
            text.configure(state='normal'); text.delete('1.0', 'end'); text.configure(state='disabled')
            status.set(f'{recording.path.name} · ' + ('Ready' if recording.next_message else 'Empty recording'))
        except Exception as exc:
            status.set(f'Cannot load recording: {exc}')
        controls()

    def tick():
        state['callback'] = None
        try:
            for _ in range(100):
                item = recording.next_message
                if item is None:
                    pause()
                    status.set(f'{recording.path.name} · End of recording')
                    return
                _, channel, message = item
                if state['mode'] == 'play':
                    delay = (message.log_time - state['anchor_ns']) / 1e9 - (time.monotonic() - state['anchor'])
                    if delay > 0:
                        state['callback'] = root.after(max(1, min(100, int(delay * 1000))), tick)
                        return
                state['position'] = message.log_time
                image_message = channel.metadata.get('shm') == 'true'
                label = f'{recording.path.name} · {(message.log_time - recording.origin) / 1e9:.3f}s · {channel.topic}'
                if image_message:
                    try:
                        image = decode_image(message.data, channel.metadata.get('compress', 'raw'), state['info'])
                        image.thumbnail((800, 480))
                        photo = ImageTk.PhotoImage(image)
                        preview.configure(image=photo, text='')
                        preview.image = photo
                    except Exception as exc:
                        preview.configure(image='', text=f'Image warning: {exc}')
                        preview.image = None
                else:
                    if channel.topic == 'camera_info':
                        try:
                            info = json.loads(message.data)
                            if isinstance(info, dict):
                                state['info'] = info
                        except (ValueError, UnicodeError):
                            pass
                    state['values'][channel.topic] = payload_text(message.data)
                    text.configure(state='normal'); text.delete('1.0', 'end')
                    text.insert('end', '\n\n'.join(f'{topic}\n{value}' for topic, value in state['values'].items()))
                    text.configure(state='disabled')
                status.set(label)
                recording.advance()
                if state['mode'] == 'step' and image_message:
                    pause()
                    return
            state['callback'] = root.after(1, tick)
        except Exception as exc:
            pause()
            status.set(f'Read error: {exc}')

    def play(mode):
        pause()
        if recording.path is None:
            return
        if recording.next_message is None:
            load(recording.path)
        if recording.next_message is None:
            return
        state.update(mode=mode, anchor=time.monotonic(), anchor_ns=state['position'])
        controls()
        state['callback'] = root.after(0, tick)

    ttk.Button(bar, text='Load', command=load).pack(side='left', padx=4)
    start_button = ttk.Button(bar, text='Start', command=lambda: play('play'))
    start_button.pack(side='left', padx=4)
    stop_button = ttk.Button(bar, text='Stop', command=pause)
    stop_button.pack(side='left', padx=4)
    step_button = ttk.Button(bar, text='Step', command=lambda: play('step'))
    step_button.pack(side='left', padx=4)

    def close():
        pause()
        recording.close()
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    controls()
    root.mainloop()
