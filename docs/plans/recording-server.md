# Recording server

Add server --config --host --port, default 0.0.0.0:9090, using FastAPI and one
Uvicorn worker. Keep main.py as dispatcher and Click as parsing helper. Reuse
Pipeline start/readiness/finish APIs for CLI and server. Load fixed YAML at
startup. Allow only one active session and stop/drain/finalize during shutdown.

Require a 1–64 character filename stem with letters, digits, underscores or
hyphens, optionally .mcap. Reject collisions and paths; never overwrite files.
Serialize lifecycle transitions; report idle/starting/recording/stopping/error.
Expose status, start, stop, list, download, per-file delete, and clear-all API.
List only regular MCAP/partial files under output_dir; reject symlinks and
traversal. Protect active files, stream opened file handles, and report bulk
failures. Preserve partials and errors; allow subsequent fresh sessions.

Serve a packaged responsive HTML/JS control page: filename, start/stop, status,
errors, table, downloads, confirmed delete/clear. Poll at one-second intervals.
Use no frontend dependencies. Trusted LAN only, no authentication as requested.

Test lifecycle, concurrent starts, failures, shutdown, payload readback,
filename conflicts, file management, active protection, traversal, and symlinks.
Add dependencies, docs, lockfile, and a VS Code server task. Verify standalone
installation and a real HTTP server. No TLS, accounts, previews, multi-session
recording, remote YAML editing, or resumable downloads.
