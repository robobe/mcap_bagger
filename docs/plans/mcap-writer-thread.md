# Dedicated MCAP writer thread

Extract MCAP ownership from Pipeline into McapWriter. Pipeline coordinates
producer startup and shutdown; only the writer thread accesses the output.

SharedQueues provides bounded put, blocking get_next, and close operations.
Alternate between nonempty queues while preserving each queue's FIFO order.
Keep existing 64 MiB and 4,096-entry limits per queue. Release the queue lock
before file operations. Preserve payloads, timestamps, sequences, and channels.

Start the writer and register channels before starting producers. On shutdown,
stop and join producers, close queue input, drain accepted entries, finalize,
flush, fsync, and publish without overwriting. Producer failures drain into a
finalized partial file. Writer failures stop production and retain partial files
without retries or footer attempts. Signal readiness and completion on failures.

Validate ordering, empty/binary payloads, blocking wakeup, queue limits, shutdown,
startup/write/finalization/publication failures, and thread cleanup. No new
compression modes, periodic sync, rotation, or legacy cleanup.
