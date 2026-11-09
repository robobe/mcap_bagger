# SHM demo and MCAP summary

Add a standalone three-slot SHM publisher using local protocol constants,
exclusive advisory locks, safe permissions, and cleanup. Generate changing
640x480 RGB8 images (921600 bytes), synthetic IMU JSON, and camera_info JSON.
Read channels and endpoints from YAML; publish at 30/100/1 Hz for ten seconds.

The one-command demo binds XPUB sockets, launches the recorder, waits up to five
seconds for subscriptions, schedules from monotonic deadlines without catch-up
bursts, and stops/finalizes the child before unlinking SHM. Identify one newly
created recording and report counts. Do not delete existing recordings.

The independent summary streams finalized MCAP files and includes empty
channels. Print counts, timestamp spans, and rates: (count-1)/(max-min), N/A
for fewer than two records or zero spans. Use FPS for images and Hz otherwise.

Test SHM interoperability, frame changes, locking, cleanup, summary calculations,
and a short full demo. Run all tests and an actual ten-second JPEG recording,
then summarize and verify decoded image dimensions. No custom dependencies,
real sensor calibration, or recorder-format changes. Delivery is not lossless.
