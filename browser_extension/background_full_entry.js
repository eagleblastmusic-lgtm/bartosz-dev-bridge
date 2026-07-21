"use strict";

// Load synchronized AUTO state, bounded Native Host result polling, then the
// recoverable replay-claim lease used by live ChatGPT rerender duplicates.
importScripts(
  "background_entry.js",
  "background_async_result.js",
  "background_auto_recovery.js"
);
