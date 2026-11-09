# Desktop MCAP test player

Use Tkinter and Pillow, no new dependencies. Add player to Click and main.py.
Provide Load, Start, Stop (pause), Step (next image and intervening text), image
preview, latest text by topic, filename/time/status. Stream in log-time order.
Use monotonic 1x playback and Tk after callbacks, at most 100 messages per tick.
Reset timing on resume; Start at EOF restarts. Close old files on reload/exit.

Decode JPEG and raw RGB8/BGR8 using encountered camera_info layout fields.
Warn rather than guess missing layouts. Bound displayed payloads to 16 KiB;
pretty-print JSON, otherwise UTF-8 or hex. No payload writes or republishing.

Test ordering, rendering, layouts, reload/cleanup, empty/invalid files, pause,
resume, stepping, and EOF. Smoke-test Tk with the existing demo MCAP. Document
manual installation of Tk if missing. No seeking, reverse, speed control,
export, or server integration. Show only the latest image in one preview.
