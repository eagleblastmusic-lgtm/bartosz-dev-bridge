"use strict";

// Load the base message router, bounded result recovery, project-launch adapter,
// client-side preflight, explicit repair correlation, then durable conversation
// correlation. Wrapper order is intentional: conversation → repair → preflight.
importScripts(
  "background_entry.js",
  "background_async_result.js",
  "background_auto_recovery.js",
  "background_project_launcher.js",
  "background_action_preflight.js",
  "background_repair_correlation.js",
  "background_conversation_binding.js"
);
