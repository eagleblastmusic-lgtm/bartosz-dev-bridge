"use strict";

// Load synchronized AUTO state, bounded Native Host result polling, recoverable
// replay claims, the Project Creator prompt handoff adapters, then the shared
// client-side action preflight.
importScripts(
  "background_entry.js",
  "background_async_result.js",
  "background_auto_recovery.js",
  "background_project_launcher.js",
  "background_action_preflight.js"
);
