# Periodic sync and legacy cleanup

Add optional finite nonnegative sync_interval_s, default 0. In the sole writer
thread use monotonic deadlines and timed queue waits. Flush MCAP chunks and
fsync dirty data at deadlines, including during idle traffic; never hold queue
locks for disk operations. Schedule from sync completion, always sync at final
shutdown, and treat periodic sync errors as writer failures retaining partials.
Require MCAP 1.6 or newer for its public flush API.

Retire the old recorder, simulation controls, legacy YAML, flight report,
interactive CLI, associated tests, fixtures, profiles, and demo publishers.
Keep only run --config, version, help, the local SHM reader, opaque pipeline,
queues, writer, and registration helper. Remove schema/protobuf dependencies
and redundant demo extras. Refresh standalone documentation and the lockfile.

Test interval validation, zero/default settings, dirty deadlines during traffic
and idle periods, timed queue behavior, failure propagation, and final sync.
Run all remaining tests in an isolated installation without sibling packages.
Periodic sync does not finalize files after crashes; recovery and rotation are
excluded. Legacy API and command compatibility are intentionally retired.
