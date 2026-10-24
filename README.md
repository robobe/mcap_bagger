# MCAP Bagger — ZMQ TCP + shared-memory images

This project is a member of the [Bullet workspace](../README.md). All six
projects share `../.venv` and `../uv.lock`; run uv commands with the
workspace flags below to retain every project and optional dependency.

Record local SHM images and ZMQ JSON telemetry into indexed MCAP files.
The shared `bullet_msgs` package owns transport validation, schemas and the SHM
reader; keep its checkout alongside this project.

## Setup and run

Requires Python 3.12 and uv.

```bash
uv sync --locked --all-packages --all-extras --all-groups
uv run --locked --all-packages --all-extras --all-groups mcap-bagger
```

At the prompt, run `init ../bullet_render/configs/recorder.yaml`, then `start`.
Use `close` to drain, finalize and print counters, and `quit` to finish. Stop/start creates separate files;
initialization cannot change during a recording.

For automatic simulation recording, run:

```bash
uv run --locked --all-packages --all-extras --all-groups mcap-bagger --config ../bullet_render/configs/recorder.yaml
```

This service needs no stdin and waits without creating a file. It subscribes to
Godot's commands/heartbeats on 5557 and physics `simulation/state` on 5555.
Start waits for physics to report `running`, then opens one new file per episode;
repeated Start does not rotate it. Joining an already running episode records its
remaining duration. Stop or Reset closes the current file without opening another.
Completion, stopped/error state, or three seconds without valid physics state also
closes it. The service stays alive for the next run. SIGINT/SIGTERM drains and
finalizes the current recording before exiting.

Closed or failed episodes are never reopened by delayed state messages. Recording
errors leave an incomplete file, do not stop physics, and allow another attempt on
the next episode. Existing command validation, controller heartbeats, sequence
checks and freshness limits apply. There are no acknowledgments or barriers:
PUB/SUB can miss commands and the beginning/end of a run. Waiting for running
state and reconnecting subscribers can delay the first captured sample.

For the standalone numbered-image demo, initialize `examples/recorder.yaml` and
start recording, then in another terminal:

```bash
uv run --locked --all-packages --all-extras --all-groups python examples/publish_demo.py --transport-config examples/transport.json
```

## Configuration and wire format

YAML requires `project`, `output_dir`, `transport_config`, and `topics`. Paths are
relative to the YAML. Project names use letters, digits, underscores and hyphens.
The transport JSON requires distinct `telemetry_endpoint`, `image_endpoint`, and `control_endpoint`,
all `tcp://127.0.0.1:<port>` (1–65535). Defaults are 5555, 5556, and 5557. In controlled mode the recorder uses all three. Physics binds
5555; both Godot and MCAP connect there. Godot binds 5556 for image descriptors;
MCAP and video_viewer connect there. No broker is needed. Subscribers reconnect
automatically and receive only future messages.

JSON publications retain exactly two parts: UTF-8 topic and payload. Image topics
use `input: shm_image` and a single descriptor notification with a NUL-terminated
exact topic. The receiver validates and privately copies pixels from one of three
locked POSIX SHM slots before queueing them. The old `raw_image` input and
multipart socket-pixel transport are rejected. Migrate image publishers together.

SHM image topics require `width`, `height`, `encoding` (`rgb8` or `bgr8`), `step`
(bytes/row) and `frame_id`. Descriptors must match the configured layout. The writer
wraps owned bytes in Foxglove RawImage Protobuf, preserving padding. Pixel payloads
must be at most 64 MiB. All image consumers require the same Linux host/user as
the publisher. See [bullet_msgs](../bullet_msgs/README.md) for the wire contract.

JSON topics require an internal-only JSON Schema path (or packaged `bullet_msgs:pose.json`) and `timestamp_source`:
An optional per-topic `endpoint` overrides the telemetry endpoint for an independent
JSON publisher. It must be loopback TCP and differ from image/control endpoints.
Topics on the same endpoint share a SUB socket, with exact topic checks.
`receive` uses Unix arrival time; `publisher` requires a positive uint64
`timestamp_ns` in the JSON payload. NaN, infinity, numeric overflow, remote schema
references, invalid UTF-8 and schema violations fail recording. Log time always
uses arrival; image publish time also uses arrival. Images and poses are independent
messages and are not exactly synchronized by the recorder.

The shared `bullet_msgs:pose.json` contract is pose version 2, including required
`linear_velocity_m_s` ([vx, vy, vz] in world m/s, Z up). The existing renderer
recorder profile automatically records this field and embeds the updated schema.
Restart the recorder with the updated publisher and rebuilt renderer together;
old pose v1 packets are rejected. Existing MCAP files keep their embedded schemas
and remain readable.

## Queues and failure behavior

A dedicated receiver owns the SUB sockets; a separate writer owns MCAP. Socket
receive high-water marks are 64 telemetry messages and 8 image messages. PUB/SUB
can drop messages for slow/disconnected subscribers before the recorder receives
them; counters cannot detect every upstream drop. The application payload queue
is limited to 64 MiB, in addition to ZeroMQ and serialization buffers.

Busy, incomplete, duplicate, overwritten or missing SHM frames are normal skips,
reported in the recording summary. A descriptor may arrive after its slot was
reused. Malformed descriptors/layouts fail recording.

Queue overflow, malformed messages, schema errors and I/O failures stop acceptance,
drain previously accepted valid data, and retain an `.mcap.partial` file. Successful
close flushes/fsyncs and publishes the completed file without replacing an existing
filename. Summaries include accepted/written counts and peak queued payload bytes.
Zero-linger sockets and finite polling permit prompt shutdown.

## Checks

```bash
uv run --locked --all-packages --all-extras --all-groups pytest -q
```

Checks cover configuration, Protobuf/JSON conversion, actual TCP/SHM delivery, exact
topics, malformed multipart messages, queue overflow, writer/finalization failures,
restart, filename collisions, command-driven lifecycle, state timeouts, stale/duplicate
commands, late joining, and signal-driven service shutdown. See the sibling renderer README for the full
physics/render/MCAP checks.

The Bullet recorder profile includes `tracking/front/bbox` from `bullet_app` on
`tcp://127.0.0.1:5558`, validated with `bullet_msgs:bbox.json`. Each processed
image yields pixel x/y/width/height or a null bbox when no target is found.
The app must run separately; the recorder never starts publishers.

## Collision completion

The render recorder profile includes raw `render/drone_1/collision` JSON on the
existing control port and confirmed `scenario/ended` on telemetry. The collision
contract is the only newly allowed JSON topic on the control endpoint.

Controlled recording accepts legacy state v1 and state v2 with terminal_result.
For a terminal result belonging to the open episode, it enqueues the result into
the same bounded writer queue before stopping, then permits a 250 ms drain for
final app/feedback packets. Results are deduplicated by event ID across direct
state ingestion and standalone event reception. Repeated terminal state does not
reopen bags; joining an already completed episode does not create a new bag.
Record `scenario/ended` in the profile to retain the full result. Queue/disk errors
retain the existing incomplete-file behavior; confirmation does not hide them.

## Flight analysis and source identity

The standard workspace launcher enables this recorder automatically. MCAP is the
flight-data source; per-process stdout/stderr logs remain available for startup,
exceptions and storage failures. No duplicate live CSV stream is written.

Every accepted SHM image queues pixels and original descriptor metadata together,
including source stamps. The writer adds `<image topic>/metadata` using the shared
`image_metadata.json` schema. `image_log_time_ns` and `image_record_sequence` link
to the image's MCAP envelope, while `metadata.session`, `metadata.sequence`,
`metadata.capture_ns` and `metadata.source` identify the original exposure.
These sequence counters serve different purposes. Skipped SHM frames create
neither an image nor an orphan metadata record. A partial write marks the entire
recording incomplete. The metadata suffix is reserved for image inputs.

Final `recording_health` MCAP metadata stores JSON-encoded fields conforming to
`recording_health.json`: accepted/written counts, skipped images, queue peak,
close outcome, authoritative episode identity, completeness and launch ID. Completeness describes recorder
acceptance/writing, not lossless upstream transport; source gaps and publisher
counters must also be inspected. Finalization failure can occur after this record
is written, so `.partial` and process logs take precedence over its complete flag.

The controlled service caches validated config/wind/spawn/renderer snapshots while
stopped and seeds the next flight with them, preserving their original source
identity/time. A snapshot may therefore refer to the preceding stopped episode;
periodic live updates follow. Mission faults are retained as `mission_fault:<reason>`
in the close outcome. Stop/Reset wait 250 ms for final app diagnostics, and terminal
success retains the existing deduplicated event drain. Recording failure leaves
`.mcap.partial`; disk failures are always reported to stderr.

`--ready-file <path>` with `--config` writes a readiness marker after control and
snapshot subscriptions are installed. It is removed on shutdown. This supports
the standard launcher; it is not a guarantee of lossless pub/sub delivery.

Offline analysis:

```bash
uv run --locked --all-packages --all-extras --all-groups python -m mcap_bagger.report \
  ../bullet_render/recordings/<flight>.mcap --output /tmp/flight-analysis --csv
```

The report uses a ±2 s arrival-time window around the first clipped-target
mission diagnostic, or the final sample if none exists. New recordings filter late foreign-episode samples by the recorded episode identity.
Exact rejected-image
matching uses processed stream/frame/capture-time/episode/physics identity; timestamps alone
never select an exact frame. Up to five nearby frames are exported as PNG.
Sensor errors compare available readings with actual physics truth within 20 ms
of simulation time. Configurations at the rejection and missing-topic warnings
are retained. Old indexed recordings remain readable. Recover/index unindexed
partial files with MCAP tooling before reporting; never overwrite the original.

| Recorded analysis data | Topic or MCAP record | Cadence |
| --- | --- | --- |
| Actual pose, motion, rotor speeds | `analysis/physics/ground_truth` | 100 Hz |
| Available IMU/barometer outputs and estimated attitude | `analysis/physics/sensors` | Physics sampling rate |
| Effective settings and revisions | `analysis/physics/config`, `analysis/app/config` | 1 Hz, plus preflight cache |
| Applied mean/gust wind | `simulation/wind_state` | 10 Hz |
| Box, processed frame, edge margins, limits and decision | Existing bbox and mission topics | Detector/mission cadence |
| Renderer drop counters and source age | `analysis/render/health` | 1 Hz |
| Original image identity and MCAP association | `<image topic>/metadata` | One per accepted image |
| Queue peak, counts, skips, outcome and completeness | `recording_health` metadata | Finalization |

For a desktop overview, the sibling [FlightLens](../flight_lens/README.md) project
opens the same indexed MCAP files directly, with linked plots, event summaries
and exact event-frame previews. It does not modify recordings or flight settings.

## MCAP directory override

For the controlled CLI, `--output-dir PATH` overrides only the YAML's
`output_dir`. It requires `--config`; it does not apply to the manual prompt.
Relative override paths resolve from the process working directory, and `~`
expands to the user's home. Without the flag, YAML-relative behavior remains.
Transport and schema paths retain the YAML file's original base directory.

```bash
uv run --locked --all-packages --all-extras --all-groups mcap-bagger \
  --config ../bullet_render/configs/recorder.yaml --output-dir /data/flights
```

The controlled service creates and checks the output directory with a temporary
write before installing control subscriptions or creating the readiness marker.
The probe is cleaned up, and failures name the offending directory and exit with
a nonzero status. This lets the standard launcher refuse to start flight services
when recording cannot begin. Later disk failures still retain partial recordings
and diagnostics using the existing behavior.

Library callers can use `Recorder.init(yaml_path, output_dir=path)` to override
the immutable loaded configuration. Initialization resolves paths without creating
directories; the controlled CLI performs preflight checks, while manual/library
recording still validates writes when starting a recording.
