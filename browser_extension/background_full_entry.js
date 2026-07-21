"use strict";

// Load the base message router, bounded result recovery, project-launch adapter,
// client-side preflight, then durable conversation/session correlation.
importScripts(
  "background_entry.js",
  "background_async_result.js",
  "background_auto_recovery.js",
  "background_project_launcher.js",
  "background_action_preflight.js",
  "background_conversation_binding.js"
);
