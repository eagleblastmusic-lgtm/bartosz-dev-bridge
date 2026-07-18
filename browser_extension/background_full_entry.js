"use strict";

// Load the existing synchronized AUTO state entrypoint first, then add bounded
// polling for Native Host responses that are initially accepted or pending.
importScripts("background_entry.js", "background_async_result.js");
