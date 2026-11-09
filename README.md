# MCAP Bagger

Standalone recording of opaque ZeroMQ payloads and shared-memory images.
Requires Python 3.12 and uv. No sibling project packages are required.

```bash
uv sync --locked
uv run --locked mcap-bagger version
uv run --locked mcap-bagger run --config config/recorder.yaml
uv run --locked pytest -q
```

YAML declares a project, output directory, and channels with `topic`, `endpoint`,
and boolean `shm`. Endpoints use loopback TCP. Output paths are relative to YAML.
Ordinary messages contain two ZeroMQ frames (topic and payload); payload bytes
are preserved, including binary and empty data. Image notifications use the
existing local SHM descriptor/header protocol. `compress: raw` preserves pixels;
`compress: jpeg` encodes JPEG at quality 90. The publisher supplies `camera_info`
as an ordinary opaque channel. No companion metadata channel is generated.

Message and image threads feed independent queues, each limited to 64 MiB and
4,096 entries. One writer alternates between queues. Overflow stops the run.
Ctrl+C or SIGTERM stops producers and drains accepted entries before finalizing.
Failures retain `.mcap.partial`; successful files use `.mcap` without overwrite.

Optional `sync_interval_s` defaults to 0 (disabled). A positive value periodically
finishes buffered MCAP chunks and fsyncs dirty data, even during idle traffic.
The sample uses 1 second. Shutdown always syncs. Sync failures stop recording.
Periodic syncing reduces unsynced data loss but does not guarantee a finalized
MCAP after a crash. Disk stalls can increase queue pressure. JPEG is lossy.

The old interactive CLI, implicit simulation mode, legacy YAML, flight report,
and legacy output/readiness options have been retired. Use `run --config` and
set output location in YAML. `register_channels` remains a Python helper for
creating registration-only files. Automated tests cover the standalone pipeline.

## Ten-second publishing example

```bash
uv run --locked python examples/run_demo.py --config config/recorder.yaml
uv run --locked python examples/summarize_mcap.py recordings/<printed-file>.mcap
```

The demo starts the recorder after binding publishers and waits for subscriptions.
It publishes changing 640x480 RGB8 SHM images at 30 FPS, synthetic IMU at 100 Hz,
and camera_info at 1 Hz for ten seconds. It keeps SHM alive until recorder
shutdown. Configured image compression is applied by the recorder. Existing
recordings are preserved; busy endpoints cause failure rather than replacement.
The summary counts all configured channels and computes observed rates from
MCAP receive timestamps using (count-1)/(last-first). Single-message and empty
channels show N/A. Both scripts accept `version` and `--help`.
