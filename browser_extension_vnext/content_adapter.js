"use strict";

const SUBMISSION_SCHEMA = "bdb-vnext-submission-v1";
const PROJECT_EXECUTION_SCHEMA = "bdb-project-execution-submission-v1";
const MAX_SUBMISSION_TEXT = 256 * 1024;
const CONTENT_ADAPTER_RUNTIME_FINGERPRINT = "bdb-vnext-content-adapter-live-sweep-v1";
const CANONICAL_RESULT_SWEEP_MS = 750;
const MAX_CANONICAL_SWEEP_BLOCKS = 256;
const MAX_CANONICAL_SCAN_NODES = 4096;
const MAX_CANONICAL_SCAN_DEPTH = 64;
const PROJECT_EXECUTION_PANEL_KIND = "project-execution";
const GENERIC_SUBMISSION_PANEL_KIND = "generic-submission";
const decorated = new WeakSet();
const executionDecorated = new WeakSet();
const decoratedPanels = new WeakMap();
const executionPanels = new WeakMap();
const projectAutoSubmissions = new Map();
let projectAutoEpoch = 0;
let projectAutoState = { phase: "awaiting_next_launch", launch_id: null, execution_binding_id: null, token: null };
const PROJECT_AUTO_STOP_MESSAGE = "bdb-vnext-project-auto-stop";
const PROJECT_EXECUTION_STATUS_MESSAGE = "bdb-vnext-project-execution-status";

function projectMessageAttribute(element, name) {
  if (!element || typeof element.getAttribute !== "function") return null;
  const value = element.getAttribute(name);
  return typeof value === "string" && value !== "" ? value : null;
}

function projectIsHTMLElement(element) {
  return typeof HTMLElement === "function" && element instanceof HTMLElement;
}

function projectIsMessageUiElement(element) {
  if (!element || (element.nodeType !== 1 && !projectIsHTMLElement(element))) return false;
  const tag = String(element.tagName || "").toLowerCase();
  const role = (projectMessageAttribute(element, "role") || "").toLowerCase();
  const className = typeof element.className === "string" ? element.className.toLowerCase() : "";
  const testId = (projectMessageAttribute(element, "data-testid") || "").toLowerCase();
  const ariaLabel = (projectMessageAttribute(element, "aria-label") || "").toLowerCase();
  return Boolean(
    ["button", "input", "textarea", "select", "option"].includes(tag) ||
    ["button", "group", "toolbar", "menu", "menuitem"].includes(role) ||
    projectMessageAttribute(element, "contenteditable") === "true" ||
    className.includes("bdb-vnext-project-launch-status") ||
    className.includes("bdb-vnext-project-execution-panel") ||
    className.includes("bdb-vnext-panel") ||
    testId.includes("copy") || testId.includes("toolbar") || testId.includes("action-bar") ||
    ariaLabel.includes("copy") || ariaLabel.includes("show more") || ariaLabel.includes("pokaż więcej")
  );
}

function projectCanonicalMessageText(owner, maximum = MAX_SUBMISSION_TEXT) {
  if (!owner) return "";
  const canonicalAttributes = ["data-message-content", "data-message-text", "data-full-text", "data-original-text"];
  for (const name of canonicalAttributes) {
    const value = projectMessageAttribute(owner, name);
    if (value !== null) return value.length <= maximum ? value.replace(/\r\n?/g, "\n") : "\u0000";
  }
  const chunks = [];
  let length = 0;
  let visited = 0;
  let oversized = false;
  const append = (value) => {
    if (oversized || typeof value !== "string" || value === "") return;
    if (length + value.length > maximum) {
      oversized = true;
      return;
    }
    chunks.push(value);
    length += value.length;
  };
  const visit = (node) => {
    if (oversized || !node) return;
    visited += 1;
    if (visited > MAX_CANONICAL_SCAN_NODES) {
      oversized = true;
      return;
    }
    if (node.nodeType === 3) {
      append(typeof node.nodeValue === "string" ? node.nodeValue : node.textContent || "");
      return;
    }
    if (node.nodeType !== 1 && !projectIsHTMLElement(node)) return;
    if (projectIsMessageUiElement(node)) return;
    if (String(node.tagName || "").toLowerCase() === "br") {
      append("\n");
      return;
    }
    const children = node.childNodes && node.childNodes.length
      ? Array.from(node.childNodes)
      : Array.from(node.children || []);
    if (children.length) {
      for (const child of children) visit(child);
    } else if (typeof node.textContent === "string") {
      append(node.textContent);
    }
  };
  visit(owner);
  return oversized ? "\u0000" : chunks.join("").replace(/\r\n?/g, "\n");
}

function canonicalSortedEvidence(refs) {
  if (!Array.isArray(refs)) return [];
  return refs.map(String).sort();
}

function canonicalCriteria(criteria) {
  if (!Array.isArray(criteria)) return [];
  return criteria.map((item) => (item && typeof item === "object" ? { ...item } : {}));
}

function parseSubmission(block) {
  const text = typeof block.textContent === "string" ? block.textContent.trim() : "";
  if (!text || text.length > MAX_SUBMISSION_TEXT || !text.includes(SUBMISSION_SCHEMA)) {
    return null;
  }
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== SUBMISSION_SCHEMA) {
      return null;
    }
    for (const field of ["submission_key", "intent_revision", "intent", "conversation_binding", "consumer_binding"]) {
      if (!(field in value)) {
        return null;
      }
    }
    if (typeof value.submission_key !== "string" || value.submission_key.length === 0) {
      return null;
    }
    return value;
  } catch (_error) {
    return null;
  }
}

function requestFromSubmission(value) {
  const request = {
    submission_key: value.submission_key,
    intent_revision: value.intent_revision,
    intent: value.intent,
    conversation_binding: value.conversation_binding,
    consumer_binding: value.consumer_binding
  };
  for (const field of ["task_id", "expected_intent_revision_id"]) {
    if (typeof value[field] === "string" && value[field].length > 0) {
      request[field] = value[field];
    }
  }
  return request;
}

function parseProjectExecutionResult(block) {
  const text = typeof block.textContent === "string" ? block.textContent.trim() : "";
  if (!text || text.length > MAX_SUBMISSION_TEXT || !text.includes(PROJECT_EXECUTION_SCHEMA)) return null;
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== PROJECT_EXECUTION_SCHEMA) return null;
    const required = [
      "project_id", "plan_version", "task_id", "execution_binding_id", "correlation_id", "command_id",
      "repo_alias", "head_before", "head_after", "execution_status", "validation_status",
      "promotion_status", "result_summary", "evidence_refs", "criteria"
    ];
    if (required.some((field) => !(field in value))) return null;
    if (typeof value.project_id !== "string" || typeof value.task_id !== "string" || typeof value.execution_binding_id !== "string") return null;
    // A complete result with malformed list fields still gets a panel so the
    // worker's final-result gate can explain the rejection before Native.
    // Project Plan may carry an integer version. The execution submission
    // contract is text, so normalize at the Browser result boundary.
    if (typeof value.plan_version === "number" && Number.isSafeInteger(value.plan_version) && value.plan_version >= 0) {
      return { ...value, plan_version: String(value.plan_version) };
    }
    return value;
  } catch (_error) {
    // YAML and prose are intentionally not accepted as a canonical result.
    return null;
  }
}

function assistantOwner(block) {
  return block.closest("[data-message-author-role='assistant']");
}

function panelIsConnected(panel) {
  if (!panel) return false;
  if (typeof panel.isConnected === "boolean") return panel.isConnected;
  return Boolean(panel.parentElement);
}

function panelMountOwner(block) {
  const owner = assistantOwner(block);
  return owner instanceof HTMLElement ? owner : null;
}

function browserResultIdentityV2(value, binding = null) {
  const b = binding || value;
  return {
    canonical_refs: value.canonical_refs && typeof value.canonical_refs === "object" ? value.canonical_refs : {},
    command_id: b.command_id || null,
    correlation_id: b.correlation_id || null,
    criteria: canonicalCriteria(value.criteria),
    evidence_refs: canonicalSortedEvidence(value.evidence_refs),
    execution_binding_id: b.execution_binding_id || null,
    execution_status: value.execution_status || null,
    failure_code: value.failure_code !== undefined ? value.failure_code : null,
    head_after: value.head_after !== undefined ? value.head_after : null,
    head_before: value.head_before !== undefined ? value.head_before : null,
    identity_version: "v2",
    plan_version: b.plan_version !== undefined && b.plan_version !== null ? String(b.plan_version) : null,
    project_id: b.project_id || null,
    promotion_status: value.promotion_status || null,
    repo_alias: b.repo_alias || null,
    result_plan_version: value.plan_version !== undefined && value.plan_version !== null ? String(value.plan_version) : null,
    result_project_id: value.project_id || null,
    result_task_id: value.task_id || null,
    summary: value.result_summary !== undefined && value.result_summary !== null ? String(value.result_summary) : "",
    task_id: b.task_id || null,
    validation_status: value.validation_status || null,
  };
}

function semanticSubmissionKey(kind, value) {
  if (kind === PROJECT_EXECUTION_PANEL_KIND) {
    if (typeof value.result_digest === "string" && value.result_digest.startsWith("sha256:")) {
      return JSON.stringify(["bdb-project-execution-result-v2", value.result_digest]);
    }
    return JSON.stringify(["bdb-project-execution-result-v2", browserResultIdentityV2(value)]);
  }
  return JSON.stringify([value.schema, value.submission_key]);
}

function setPanelIdentity(panel, kind, key) {
  panel.dataset.bdbSubmissionKind = kind;
  panel.dataset.bdbSubmissionKey = key;
}

function panelMatchesIdentity(panel, panelClass, kind, key) {
  return Boolean(
    panel instanceof HTMLElement &&
    panel.className === panelClass &&
    panel.dataset &&
    panel.dataset.bdbSubmissionKind === kind &&
    panel.dataset.bdbSubmissionKey === key
  );
}

function connectedPanelsFor(owner, panelClass, kind, key) {
  const matches = [];
  for (const candidate of Array.from(owner.children || [])) {
    if (panelIsConnected(candidate) && panelMatchesIdentity(candidate, panelClass, kind, key)) {
      matches.push(candidate);
    }
  }
  return matches;
}

function removePanel(panel) {
  if (typeof panel.remove === "function") {
    panel.remove();
  } else if (panel.parentElement && typeof panel.parentElement.removeChild === "function") {
    panel.parentElement.removeChild(panel);
  }
}

function reusePanel(block, panelClass, kind, key, panels, decoratedBlocks) {
  const owner = panelMountOwner(block);
  if (!owner) return false;
  const previous = panels.get(block);
  const matches = connectedPanelsFor(owner, panelClass, kind, key);
  if (matches.length > 0) {
    const canonical = panelIsConnected(previous) && matches.includes(previous) ? previous : matches[0];
    for (const duplicate of matches) {
      if (duplicate !== canonical) removePanel(duplicate);
    }
    panels.set(block, canonical);
    decoratedBlocks.add(block);
    return true;
  }
  return false;
}

function mountPanel(block, panel, panelClass, kind, key, panels, decoratedBlocks) {
  if (reusePanel(block, panelClass, kind, key, panels, decoratedBlocks)) return false;
  const owner = panelMountOwner(block);
  if (!owner) return false;
  setPanelIdentity(panel, kind, key);
  if (typeof owner.appendChild === "function") {
    owner.appendChild(panel);
  } else if (typeof owner.append === "function") {
    owner.append(panel);
  } else {
    return false;
  }
  panels.set(block, panel);
  decoratedBlocks.add(block);
  return true;
}

function setResult(output, message, state = "neutral") {
  output.textContent = message;
  output.dataset.state = state;
}

function decorate(block, submission) {
  const panelClass = "bdb-vnext-panel";
  const panelKind = GENERIC_SUBMISSION_PANEL_KIND;
  const panelKey = semanticSubmissionKey(panelKind, submission);
  if (reusePanel(block, panelClass, panelKind, panelKey, decoratedPanels, decorated)) {
    return;
  }
  const panel = document.createElement("div");
  panel.className = panelClass;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-vnext-submit";
  button.textContent = "BDB vNext: Submit";
  button.setAttribute("aria-label", "Submit this request to the canonical BDB vNext generation");
  const output = document.createElement("div");
  output.className = "bdb-vnext-output";
  output.setAttribute("role", "status");
  output.setAttribute("aria-live", "polite");

  button.addEventListener("click", async () => {
    button.disabled = true;
    setResult(output, "Submitting through canonical vNext transport…");
    try {
      const response = await chrome.runtime.sendMessage({
        type: "bdb-vnext-submit",
        request: requestFromSubmission(submission)
      });
      if (response && response.ok === true && response.receipt) {
        const taskId = response.receipt.task_id || "accepted";
        setResult(output, `Accepted by vNext: ${taskId}`, "success");
        button.textContent = "BDB vNext: Accepted";
        return;
      }
      if (response && response.uncertain === true) {
        setResult(output, "Delivery is uncertain. Use BDB vNext Resume/lookup; do not create a new submission.", "warning");
        button.textContent = "BDB vNext: Lookup required";
        return;
      }
      throw new Error(response && response.error ? response.error : "vNext submission failed closed");
    } catch (error) {
      setResult(output, error instanceof Error ? error.message : String(error), "error");
      button.textContent = "BDB vNext: Retry same request";
      button.disabled = false;
    }
  });

  panel.append(button, output);
  mountPanel(block, panel, panelClass, panelKind, panelKey, decoratedPanels, decorated);
}

async function projectExecutionStatusFor(projectId, executionBindingId, conversationId) {
  if (!projectId || !executionBindingId || !conversationId) return null;
  try {
    const response = await chrome.runtime.sendMessage({
      type: PROJECT_EXECUTION_STATUS_MESSAGE,
      project_id: projectId,
      execution_binding_id: executionBindingId,
      conversation_id: conversationId
    });
    return response && response.ok === true && response.response && response.response.status === "project_execution_status"
      ? response.response
      : null;
  } catch (_error) {
    return null;
  }
}

function projectAutoStop(reason) {
  const previous = projectAutoState;
  projectAutoEpoch += 1;
  projectAutoState = {
    phase: "stopped",
    launch_id: previous.launch_id || null,
    execution_binding_id: previous.execution_binding_id || null,
    token: String(reason || "stopped")
  };
}

function projectAutoGateMatches(status, result, conversationId) {
  const binding = status && status.binding;
  const auto = status && status.milestone_auto;
  return Boolean(
    status &&
    status.current_binding_id === result.execution_binding_id &&
    status.current_task_id === result.task_id &&
    binding &&
    binding.project_id === result.project_id &&
    binding.execution_binding_id === result.execution_binding_id &&
    binding.task_id === result.task_id &&
    binding.conversation_id === conversationId &&
    binding.status === "ACTIVE" &&
    binding.superseded !== true &&
    auto &&
    auto.status === "RUNNABLE" &&
    auto.current_task_id === result.task_id &&
    typeof auto.milestone_run_id === "string" &&
    auto.milestone_run_id.length > 0
  );
}

function projectCanonicalHandoffSentMatches(status, launch, conversationId) {
  const binding = status && status.binding;
  const handoff = status && status.launch_handoff;
  return Boolean(
    status && status.current_binding_id === launch.execution_binding_id && status.current_task_id === launch.task_id &&
    binding && binding.status === "ACTIVE" && binding.superseded !== true &&
    binding.project_id === launch.project_id && binding.execution_binding_id === launch.execution_binding_id &&
    binding.plan_version === String(launch.plan_version) && binding.task_id === launch.task_id &&
    binding.launch_id === launch.launch_id && binding.correlation_id === launch.correlation_id &&
    binding.command_id === launch.command_id && binding.repo_alias === launch.repo_alias &&
    binding.expected_repo_head_before === launch.expected_repo_head_before &&
    binding.conversation_id === conversationId &&
    handoff && handoff.status === "SENT" && handoff.project_id === launch.project_id &&
    handoff.execution_binding_id === launch.execution_binding_id && handoff.task_id === launch.task_id &&
    handoff.launch_id === launch.launch_id && handoff.conversation_id === conversationId &&
    (status.launch_outbox_status === "PUBLISHED" || status.launch_outbox_status === "ACKNOWLEDGED")
  );
}

function projectCanonicalDeliveryAckMatches(status, launch, conversationId) {
  return projectCanonicalHandoffSentMatches(status, launch, conversationId) && status.launch_outbox_status === "ACKNOWLEDGED";
}

function projectExecutionResultRefs(panel, button, output) {
  return { panel, button, output };
}

function projectResultFailureText(response) {
  const code = response && typeof response.error_code === "string" ? response.error_code : null;
  const message = response && typeof response.error === "string" ? response.error : "project execution result rejected";
  if (code === "execution_result_non_terminal") {
    return `${code}: ${message}. Final submission was withheld; wait for validation and submit a final result on this binding.`;
  }
  if (code === "execution_field_invalid" || code === "execution_schema_invalid" || code === "execution_status_invalid") {
    return `${code}: ${message}. Correct the result JSON and retry; the rejected result did not change Project Memory.`;
  }
  return code ? `${code}: ${message}. Check canonical execution status before retrying.` : message;
}

async function submitProjectExecutionResult(block, result, refs, { automatic = false, gate = null } = {}) {
  const { button, output } = refs;
  const conversationId = projectConversationId();
  if (!conversationId) {
    setResult(output, "Project execution requires a canonical conversation.", "error");
    return false;
  }
  if (automatic && !projectAutoGateMatches(gate, result, conversationId)) {
    projectAutoStop("canonical_auto_gate_rejected");
    return false;
  }
  const autoEpoch = projectAutoEpoch;
  if (automatic && projectAutoState.phase === "stopped") return false;
  const request = {
    type: "bdb-vnext-project-execution-submit",
    result,
    conversation_id: conversationId
  };
  // Native resolves the launch from the canonical binding; local Browser
  // projections may disappear on reload and must not supply authority.
  button.disabled = true;
  setResult(output, automatic ? "BDB vNext: Submitting…" : "Submitting project result through canonical vNext transport…");
  try {
    if (automatic && (autoEpoch !== projectAutoEpoch || projectAutoState.phase === "stopped")) {
      button.disabled = false;
      return false;
    }
    const response = await chrome.runtime.sendMessage(request);
    if (response && response.ok === true && response.receipt) {
      const receipt = response.receipt;
      const next = receipt.current_task_id ? ` · Next: ${receipt.current_task_id}` : "";
      const resultStatus = String(receipt.result_status || "UNKNOWN").toUpperCase();
      const acceptedPass = receipt.accepted === true && resultStatus === "PASS";
      const blocked = receipt.task_status === "blocked";
      const label = acceptedPass
        ? "BDB vNext: Result accepted"
        : blocked
          ? "BDB vNext: Result blocked"
          : resultStatus === "REVIEW_REQUIRED"
            ? "BDB vNext: Review required"
            : resultStatus === "UNKNOWN"
              ? "BDB vNext: Result unknown"
              : "BDB vNext: Result failed";
      const prefix = receipt.replayed ? `Replayed ${resultStatus}` : acceptedPass ? "Accepted" : "Failed";
      const suffix = blocked ? " · blocked" : next;
      setResult(output, `${prefix}: ${receipt.task_id}${suffix}`, acceptedPass ? "success" : "error");
      button.textContent = label;
      if (automatic) {
        if (autoEpoch !== projectAutoEpoch || projectAutoState.phase === "stopped") {
          projectAutoStop("user_stop_during_result_submit");
          return true;
        }
        if (!acceptedPass) {
          projectAutoStop("result_not_accepted");
          return true;
        }
        projectAutoState = {
          phase: "result_accepted",
          launch_id: gate && gate.binding ? gate.binding.launch_id : null,
          execution_binding_id: result.execution_binding_id,
          token: receipt.replayed ? "replayed" : "accepted"
        };
        const nextLaunch = receipt.next_launch;
        if (acceptedPass && receipt.milestone_status === "RUNNABLE" && nextLaunch && nextLaunch.project_id === result.project_id && nextLaunch.task_id && nextLaunch.execution_binding_id) {
          projectAutoState.phase = "awaiting_next_launch";
          void projectHandleLaunch(nextLaunch, { automatic: true }).catch(() => projectAutoStop("next_launch_failed"));
        } else if (receipt.next_launch_status === "already_sent" || receipt.milestone_status === "MILESTONE_COMPLETED") {
          projectAutoState.phase = receipt.milestone_status === "MILESTONE_COMPLETED" ? "stopped" : "sent";
        } else {
          projectAutoStop(receipt.milestone_status === "RUNNABLE" ? "next_launch_recovery_required" : "next_launch_not_runnable");
        }
      }
      return true;
    }
    if (response && response.error_code === "execution_result_non_terminal") {
      setResult(output, projectResultFailureText(response), "warning");
      button.textContent = "BDB vNext: Await validation";
      if (automatic) projectAutoState.phase = "waiting_external";
      return false;
    }
    throw new Error(projectResultFailureText(response));
  } catch (error) {
    setResult(output, error instanceof Error ? error.message : String(error), "error");
    button.textContent = "BDB vNext: Retry result";
    button.disabled = false;
    if (automatic) projectAutoState.phase = "error";
    return false;
  }
}

async function autoSubmitProjectExecution(block, result, refs, panelKey) {
  if (projectAutoSubmissions.has(panelKey)) return;
  const record = { status: "checking" };
  projectAutoSubmissions.set(panelKey, record);
  try {
    projectAutoState = {
      phase: "detected",
      launch_id: null,
      execution_binding_id: result.execution_binding_id,
      token: panelKey
    };
    const conversationId = projectConversationId();
    const status = await projectExecutionStatusFor(result.project_id, result.execution_binding_id, conversationId);
    if (!status) {
      record.status = "manual";
      return;
    }
    if (!projectAutoGateMatches(status, result, conversationId)) {
      record.status = "stopped";
      return;
    }
    projectAutoState = {
      phase: "submitting_result",
      launch_id: status.binding.launch_id,
      execution_binding_id: result.execution_binding_id,
      token: panelKey
    };
    record.status = "submitting";
    record.result = await submitProjectExecutionResult(block, result, refs, { automatic: true, gate: status });
    record.status = record.result ? "accepted" : "error";
  } catch (_error) {
    record.status = "error";
  }
}

function decorateProjectExecution(block, result) {
  const panelClass = "bdb-vnext-project-execution-panel";
  const panelKind = PROJECT_EXECUTION_PANEL_KIND;
  const panelKey = semanticSubmissionKey(panelKind, result);
  if (reusePanel(block, panelClass, panelKind, panelKey, executionPanels, executionDecorated)) return;
  const panel = document.createElement("div");
  panel.className = panelClass;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "bdb-vnext-project-execution-submit";
  button.textContent = "BDB vNext: Submit result";
  button.setAttribute("aria-label", "Submit this project execution result to canonical BDB Project Memory");
  const output = document.createElement("div");
  output.className = "bdb-vnext-project-execution-output";
  output.setAttribute("role", "status");
  output.setAttribute("aria-live", "polite");
  button.addEventListener("click", async () => {
    await submitProjectExecutionResult(block, result, projectExecutionResultRefs(panel, button, output));
  });
  panel.append(button, output);
  mountPanel(block, panel, panelClass, panelKind, panelKey, executionPanels, executionDecorated);
  void autoSubmitProjectExecution(block, result, projectExecutionResultRefs(panel, button, output), panelKey);
}

// Project launch is a transport handoff from the canonical GUI. It never
// creates BDB semantic state and it never submits the ChatGPT form.
const PROJECT_LAUNCH_SCHEMA = "bdb-project-launch-v1";
const PROJECT_BINDINGS_KEY = "bdbVnextProjectLaunchBindingsV1";
const PROJECT_BINDINGS_LIMIT = 128;
const PROJECT_POLL_MS = 1200;
const PROJECT_LEASE_SECONDS = 30;
const PROJECT_LAUNCH_INSERT_MESSAGE = "bdb-vnext-project-launch-insert";
const PROJECT_UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const PROJECT_TAB_INSTANCE_KEY = "bdbVnextProjectTabInstanceV1";
let projectPollActive = false;
let projectInsertionActive = false;
const projectClaims = new Map();
let projectTabInstance;

function projectConversationId() {
  if (location.protocol !== "https:" || location.hostname !== "chatgpt.com") return null;
  const match = location.pathname.match(/(?:^|\/)c\/([A-Za-z0-9-]{8,128})(?:\/|$)/);
  return match ? match[1] : null;
}

function projectPageEligible({ selectedByUser = false } = {}) {
  return Boolean(
    projectConversationId() &&
    document.visibilityState === "visible"
  );
}

function projectVisible(element) {
  if (!(element instanceof HTMLElement)) return false;
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
}

function projectFindComposer() {
  const selectors = [
    "#prompt-textarea",
    "textarea[data-testid='textbox']",
    "textarea[placeholder*='Message']",
    "[contenteditable='true'][role='textbox']",
    "[contenteditable='true']"
  ];
  for (const selector of selectors) {
    const found = [];
    for (const element of document.querySelectorAll(selector)) {
      if (!projectVisible(element)) continue;
      const messageOwner = typeof element.closest === "function"
        ? element.closest("[data-message-author-role='assistant'], [data-message-author-role='user']")
        : null;
      if (messageOwner) continue;
      found.push(element);
    }
    if (found.length === 1) return found[0];
    if (found.length > 1) return null;
  }
  return null;
}

function projectComposerText(composer) {
  if (!composer) return null;
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    return composer.value;
  }
  return typeof composer.innerText === "string" ? composer.innerText : composer.textContent || "";
}

function projectComposerHasForeignState(composer) {
  if (!composer) return true;
  if (projectComposerText(composer).trim() !== "") return true;
  return Boolean(composer.querySelector(
    "input[type='file'], [data-testid*='attachment'], [data-file-id], [aria-label*='attachment' i], [aria-label*='file' i]"
  ));
}

function projectClaimId(launchId) {
  let value = projectClaims.get(launchId);
  if (!value) {
    value = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    projectClaims.set(launchId, value);
  }
  return value;
}

function projectTabInstanceId() {
  if (projectTabInstance) return projectTabInstance;
  try {
    const existing = sessionStorage.getItem(PROJECT_TAB_INSTANCE_KEY);
    if (typeof existing === "string" && PROJECT_UUID_RE.test(existing)) {
      projectTabInstance = existing;
      return projectTabInstance;
    }
    projectTabInstance = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    sessionStorage.setItem(PROJECT_TAB_INSTANCE_KEY, projectTabInstance);
  } catch (_error) {
    projectTabInstance = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return projectTabInstance;
}

function projectValidLaunch(value) {
  return Boolean(
    value && typeof value === "object" && !Array.isArray(value) &&
    value.schema === PROJECT_LAUNCH_SCHEMA &&
    typeof value.launch_id === "string" && PROJECT_UUID_RE.test(value.launch_id) &&
    typeof value.repo_alias === "string" && /^[a-z][a-z0-9-]{0,31}$/.test(value.repo_alias) &&
    typeof value.prompt === "string" && value.prompt.trim() !== "" && value.prompt.length <= 50000 &&
    typeof value.auto_send === "boolean" && typeof value.created_at === "string" && typeof value.expires_at === "string"
  );
}

async function projectReadBindings() {
  try {
    const stored = await chrome.storage.local.get(PROJECT_BINDINGS_KEY);
    const value = stored[PROJECT_BINDINGS_KEY];
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const sanitized = {};
    for (const [k, v] of Object.entries(value)) {
      if (
        v &&
        typeof v === "object" &&
        !Array.isArray(v) &&
        typeof v.launch_id === "string" &&
        v.launch_id.trim() !== ""
      ) {
        sanitized[k] = v;
      }
    }
    return sanitized;
  } catch (_error) {
    return {};
  }
}

async function projectWriteBinding(launch, claimId, conversationId, state, extra = {}) {
  const bindings = await projectReadBindings();
  const now = Date.now();
  bindings[launch.launch_id] = {
    schema: "bdb-vnext-project-launch-binding-v1",
    launch_id: launch.launch_id,
    conversation_id: conversationId,
    tab_instance_id: projectTabInstanceId(),
    claim_id: claimId,
    repo_alias: launch.repo_alias,
    project_id: launch.project_id || null,
    plan_version: launch.plan_version !== undefined && launch.plan_version !== null ? String(launch.plan_version) : null,
    task_id: launch.task_id || null,
    execution_binding_id: launch.execution_binding_id || null,
    correlation_id: launch.correlation_id || null,
    command_id: launch.command_id || null,
    expected_repo_head_before: launch.expected_repo_head_before || null,
    auto_send: launch.auto_send === true,
    state,
    ...extra,
    updated_at: now
  };
  const entries = Object.entries(bindings)
    .filter(([, value]) => value && typeof value === "object" && Number.isFinite(value.updated_at))
    .sort((left, right) => left[1].updated_at - right[1].updated_at)
    .slice(-PROJECT_BINDINGS_LIMIT);
  await chrome.storage.local.set({ [PROJECT_BINDINGS_KEY]: Object.fromEntries(entries) });
}

function projectBindingFor(bindings, launchId, conversationId) {
  const value = bindings[launchId];
  return value && value.conversation_id === conversationId && value.tab_instance_id === projectTabInstanceId() && PROJECT_UUID_RE.test(value.claim_id || "") ? value : null;
}

function projectAnnounce(message, state = "neutral") {
  let output = document.querySelector(".bdb-vnext-project-launch-status");
  if (!(output instanceof HTMLElement)) {
    output = document.createElement("div");
    output.className = "bdb-vnext-project-launch-status";
    output.setAttribute("role", "status");
    output.setAttribute("aria-live", "polite");
    const composer = projectFindComposer();
    if (composer) composer.insertAdjacentElement("beforebegin", output);
  }
  output.textContent = message;
  output.dataset.state = state;
}

function projectExactPromptHtml(prompt) {
  return prompt
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
    .replaceAll("\n", "<br>");
}

function projectInsertExact(composer, prompt) {
  if (!composer || projectComposerHasForeignState(composer)) return false;
  composer.focus();
  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    const prototype = Object.getPrototypeOf(composer);
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (!setter) return false;
    setter.call(composer, prompt);
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
    composer.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    let inserted = false;
    try {
      inserted = document.execCommand("insertHTML", false, projectExactPromptHtml(prompt));
    } catch (_error) {
      inserted = false;
    }
    if (!inserted || projectComposerText(composer) !== prompt) {
      composer.textContent = prompt;
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: prompt }));
    }
  }
  return projectComposerText(composer) === prompt;
}

function projectFindSendControl(composer) {
  if (!composer) return null;
  const form = typeof composer.closest === "function" ? composer.closest("form") : null;
  const roots = form ? [form, document] : [document];
  const selectors = [
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label*='Send' i]",
    "button[type='submit']"
  ];
  for (const root of roots) {
    for (const selector of selectors) {
      const matches = typeof root.querySelectorAll === "function" ? Array.from(root.querySelectorAll(selector)).filter(projectVisible) : [];
      if (matches.length === 1) return matches[0];
      if (matches.length > 1 && selector === "button[data-testid='send-button']") return null;
    }
  }
  return null;
}

function projectExactUserMessageCount(prompt) {
  if (typeof prompt !== "string" || prompt === "") return 0;
  const expected = prompt.replace(/\r\n?/g, "\n");
  let count = 0;
  for (const node of document.querySelectorAll("[data-message-author-role='user']")) {
    if (!(node instanceof HTMLElement)) continue;
    if (!projectVisible(node) || projectIsMessageUiElement(node)) continue;
    if (projectCanonicalMessageText(node, Math.max(MAX_SUBMISSION_TEXT, expected.length)) === expected) count += 1;
  }
  return count;
}

function projectSendControlEnabled(send) {
  return Boolean(
    send && send.disabled !== true &&
    (typeof send.getAttribute !== "function" || send.getAttribute("aria-disabled") !== "true")
  );
}

function projectSendEffectObserved(prompt, baselineCount, conversationId) {
  if (!conversationId || projectConversationId() !== conversationId) return false;
  const composer = projectFindComposer();
  if (!composer || projectComposerText(composer) !== "") return false;
  return projectExactUserMessageCount(prompt) > baselineCount;
}

function projectDelay(milliseconds) {
  if (typeof setTimeout === "function") {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }
  return Promise.resolve();
}

async function projectVerifyInsertedStable(prompt, {
  conversationId = null,
  selectedByUser = false
} = {}) {
  const observations = 3;
  for (let observation = 0; observation < observations; observation += 1) {
    if (observation > 0) await projectDelay(150);
    const sameSelection = Boolean(conversationId && projectConversationId() === conversationId);
    if (!projectPageEligible({ selectedByUser }) || !sameSelection) return false;
    const composer = projectFindComposer();
    if (!composer || projectComposerText(composer) !== prompt) return false;
  }
  return true;
}

async function projectAutoSendInserted(launch, claimId, prompt, insertedComposer, token) {
  if (projectAutoState.phase === "stopped") return false;
  const epoch = projectAutoEpoch;
  projectAutoState = { phase: "awaiting_send_ready", launch_id: launch.launch_id, execution_binding_id: launch.execution_binding_id, token };
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (epoch !== projectAutoEpoch || projectAutoState.phase === "stopped") return false;
    const composer = projectFindComposer();
    if (!composer || projectComposerText(composer) !== prompt) {
      projectAutoStop("composer_changed_before_send");
      projectAnnounce("BDB AUTO zatrzymane: composer został zmieniony przez użytkownika.", "warning");
      return false;
    }
    const status = await projectExecutionStatusFor(launch.project_id, launch.execution_binding_id, projectConversationId());
    if (!status || status.current_binding_id !== launch.execution_binding_id || status.current_task_id !== launch.task_id || !status.milestone_auto || status.milestone_auto.status !== "RUNNABLE" || status.binding?.conversation_id !== projectConversationId()) {
      projectAutoStop("canonical_auto_send_gate_rejected");
      projectAnnounce("BDB AUTO zatrzymane: canonical milestone nie jest już RUNNABLE.", "warning");
      return false;
    }
    const send = projectFindSendControl(composer);
    if (projectSendControlEnabled(send) && projectComposerText(composer) === prompt && epoch === projectAutoEpoch) {
      const conversationId = projectConversationId();
      const baselineCount = projectExactUserMessageCount(prompt);
      projectAutoState.phase = "sending_prompt";
      if (typeof send.click !== "function") return false;
      await projectWriteBinding(launch, claimId, conversationId, "SEND_ATTEMPTED", {
        send_baseline_count: baselineCount,
        send_attempt_token: token
      });
      try {
        send.click();
      } catch (_error) {
        projectAutoState.phase = "error";
        projectAnnounce("BDB AUTO zatrzymane: kontrolka Send odrzuciła próbę.", "warning");
        return false;
      }
      projectAutoState.phase = "verifying_send_effect";
      for (let verifyAttempt = 0; verifyAttempt < 50; verifyAttempt += 1) {
        if (epoch !== projectAutoEpoch || projectAutoState.phase === "stopped") return false;
        if (projectSendEffectObserved(prompt, baselineCount, conversationId)) {
          await projectWriteBinding(launch, claimId, conversationId, "SEND_CONFIRMED", {
            send_baseline_count: baselineCount,
            send_attempt_token: token
          });
          projectAutoState.phase = "sent";
          projectAnnounce("BDB AUTO: wysłanie promptu potwierdzone.", "success");
          return true;
        }
        await projectDelay(100);
      }
      projectAutoState.phase = "error";
      projectAnnounce("BDB AUTO zatrzymane: brak potwierdzonego efektu Send; ponowna próba jest zablokowana.", "warning");
      return false;
    }
    await projectDelay(100);
  }
  projectAutoState.phase = "error";
  projectAnnounce("BDB AUTO zatrzymane: kontrolka Send nie stała się dostępna.", "warning");
  return false;
}

async function projectPeek() {
  const result = await chrome.runtime.sendMessage({ type: "bdb-vnext-project-launch-peek" });
  if (!result || result.ok !== true || !result.response || result.response.status !== "project_launch") return null;
  return projectValidLaunch(result.response.launch) ? result.response.launch : null;
}

async function projectClaim(launch, claimId, conversationId) {
  const result = await chrome.runtime.sendMessage({
    type: "bdb-vnext-project-launch-claim",
    launch_id: launch.launch_id,
    claim_id: claimId,
    ...(conversationId ? { conversation_id: conversationId } : {})
  });
  if (!result || result.ok !== true || !result.response || result.response.status !== "claimed") return null;
  return projectValidLaunch(result.response.launch) ? result.response.launch : null;
}

async function projectAck(launchId, claimId, conversationId = null, handoff = null) {
  let explicitConversation = null;
  let handoffObj = handoff;
  if (typeof conversationId === "string") {
    explicitConversation = conversationId;
  } else if (conversationId && typeof conversationId === "object" && handoff === null) {
    handoffObj = conversationId;
    explicitConversation = handoffObj.conversation_id || null;
  }
  const finalConversation = (handoffObj && handoffObj.conversation_id) || explicitConversation;
  const result = await chrome.runtime.sendMessage({
    type: "bdb-vnext-project-launch-ack",
    launch_id: launchId,
    claim_id: claimId,
    ...(finalConversation ? { conversation_id: finalConversation } : {}),
    ...(handoffObj ? { handoff: handoffObj } : {})
  });
  return Boolean(result && result.ok === true && result.response && result.response.status === "acknowledged");
}

function projectLaunchResult(ok, code, launchId = null) {
  const result = { ok: ok === true, code };
  if (typeof launchId === "string" && launchId.length > 0) result.launch_id = launchId;
  return result;
}

async function projectHandleLaunch(launch, { selectedByUser = false, automatic = false } = {}) {
  const launchId = launch?.launch_id || null;
  if (projectInsertionActive || !projectPageEligible({ selectedByUser })) {
    return projectLaunchResult(false, "project_prompt_not_inserted", launchId);
  }
  const conversationId = projectConversationId();
  if (!conversationId) {
    return projectLaunchResult(false, "conversation_not_eligible", launchId);
  }
  const autoMode = automatic || launch.auto_send === true;
  const composer = projectFindComposer();
  if (!composer) {
    return projectLaunchResult(false, "project_prompt_not_inserted", launchId);
  }
  const bindings = await projectReadBindings();
  const existing = conversationId ? projectBindingFor(bindings, launch.launch_id, conversationId) : null;
  const canonicalBeforeClaim = autoMode
    ? await projectExecutionStatusFor(launch.project_id, launch.execution_binding_id, conversationId)
    : null;
  if (autoMode && projectCanonicalDeliveryAckMatches(canonicalBeforeClaim, launch, conversationId)) {
    const claimId = existing ? existing.claim_id : projectClaimId(launch.launch_id);
    // Native consumes any stale queue projection when the canonical outbox is
    // already acknowledged. The Browser cache is rebuilt only as a cache.
    await projectClaim(launch, claimId, conversationId);
    try { await projectWriteBinding(launch, claimId, conversationId, "ACKED"); } catch (_error) { /* canonical ACK is sufficient */ }
    projectAutoState = { phase: "sent", launch_id: launch.launch_id, execution_binding_id: launch.execution_binding_id, token: "canonical-ack" };
    projectAnnounce("BDB AUTO: kanoniczny ACK potwierdza wysyłkę; wznowiono bez ponownego Send.", "success");
    return projectLaunchResult(true, "project_prompt_inserted", launchId);
  }
  const localSendProof = existing && (existing.state === "SEND_ATTEMPTED" || existing.state === "SEND_CONFIRMED");
  if (autoMode && !localSendProof && !projectCanonicalHandoffSentMatches(canonicalBeforeClaim, launch, conversationId) && projectExactUserMessageCount(launch.prompt) > 0 && projectComposerText(composer) === "") {
    projectAnnounce("BDB AUTO zatrzymane: prompt jest w rozmowie, lecz brak kanonicznego ACK. Ponowny Send jest niebezpieczny; wymagane uzgodnienie dostawy.", "warning");
    return projectLaunchResult(false, "project_auto_duplicate_guard", launchId);
  }
  if (!existing && projectComposerHasForeignState(composer)) {
    if (projectComposerText(composer) === launch.prompt) {
      // Storage may have been lost after insertion but before ACK. The exact
      // canonical pending prompt is safe to verify and acknowledge without re-inserting.
    } else {
      projectAnnounce("BDB vNext: composer is not empty; launch left pending.", "warning");
      return projectLaunchResult(false, "project_prompt_not_inserted", launchId);
    }
  }
  const claimId = existing ? existing.claim_id : projectClaimId(launch.launch_id);
  if (autoMode && !selectedByUser) {
    const ownership = await projectExecutionStatusFor(launch.project_id, launch.execution_binding_id, conversationId);
    if (!ownership?.binding || ownership.binding.conversation_id !== conversationId) {
      projectAnnounce("BDB AUTO: wybierz rozmowę przez przycisk rozszerzenia; automatyczny odbiór wymaga kanonicznego przypisania rozmowy.", "warning");
      return projectLaunchResult(false, "project_conversation_selection_required", launchId);
    }
  }
  const claimed = await projectClaim(launch, claimId, conversationId);
  if (!claimed) return projectLaunchResult(false, "project_prompt_not_inserted", launchId);
  const claimedLaunchId = claimed.launch_id || launchId;
  const sameSelection = projectConversationId() === conversationId;
  if (!projectPageEligible({ selectedByUser }) || !sameSelection) {
    return projectLaunchResult(false, "project_prompt_not_inserted", claimedLaunchId);
  }
  let canonicalStatus = null;
  if (autoMode) {
    canonicalStatus = await projectExecutionStatusFor(claimed.project_id, claimed.execution_binding_id, conversationId);
    if (!canonicalStatus || canonicalStatus.current_binding_id !== claimed.execution_binding_id || canonicalStatus.current_task_id !== claimed.task_id || !canonicalStatus.binding || canonicalStatus.binding.project_id !== claimed.project_id || canonicalStatus.binding.task_id !== claimed.task_id || canonicalStatus.binding.conversation_id !== conversationId || !canonicalStatus.milestone_auto || canonicalStatus.milestone_auto.status !== "RUNNABLE") {
      projectAutoStop("canonical_launch_gate_rejected");
      return projectLaunchResult(false, "project_auto_gate_rejected", claimedLaunchId);
    }
  }
  if (autoMode && projectCanonicalHandoffSentMatches(canonicalStatus, claimed, conversationId)) {
    const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
    return projectLaunchResult(acknowledged, acknowledged ? "project_prompt_inserted" : "project_prompt_ack_failed", claimedLaunchId);
  }
  if (existing?.state === "ACKED" && !autoMode) {
    return projectLaunchResult(true, "project_prompt_inserted", claimedLaunchId);
  }
  if (existing?.state === "ACKED" && existing.auto_send === true) {
    projectAnnounce("BDB AUTO: RECOVERY_REQUIRED — lokalny ACK nie zgadza się z potwierdzeniem wysyłki. Ponowna wysyłka zablokowana.", "warning");
    return projectLaunchResult(false, "project_auto_send_uncertain", claimedLaunchId);
  }
  if (autoMode && existing?.state === "SEND_CONFIRMED") {
    const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
    if (acknowledged) {
      await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
      return projectLaunchResult(true, "project_prompt_inserted", claimedLaunchId);
    }
    return projectLaunchResult(false, "project_prompt_ack_failed", claimedLaunchId);
  }
  if (autoMode && existing?.state === "SEND_ATTEMPTED") {
    const baseline = existing.send_baseline_count;
    if (Number.isInteger(baseline) && baseline >= 0 && projectSendEffectObserved(claimed.prompt, baseline, conversationId)) {
      await projectWriteBinding(claimed, claimId, conversationId, "SEND_CONFIRMED", {
        send_baseline_count: baseline,
        send_attempt_token: existing.send_attempt_token || null
      });
      const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
      if (acknowledged) {
        await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
        return projectLaunchResult(true, "project_prompt_inserted", claimedLaunchId);
      }
      return projectLaunchResult(false, "project_prompt_ack_failed", claimedLaunchId);
    }
    projectAnnounce("BDB AUTO zatrzymane: poprzednia próba Send pozostaje niepewna; duplicate Send zablokowany.", "warning");
    return projectLaunchResult(false, "project_auto_send_uncertain", claimedLaunchId);
  }
  if (conversationId) await projectWriteBinding(claimed, claimId, conversationId, "CLAIMED");
  projectInsertionActive = true;
  try {
    const currentComposer = projectFindComposer();
    const currentText = projectComposerText(currentComposer);
    if (currentText !== claimed.prompt) {
      if (projectComposerHasForeignState(currentComposer) || !projectInsertExact(currentComposer, claimed.prompt)) {
        projectAnnounce("BDB vNext: launch not inserted; composer changed.", "warning");
        return projectLaunchResult(false, "project_prompt_not_inserted", claimedLaunchId);
      }
    }
    const stableInsertion = await projectVerifyInsertedStable(claimed.prompt, {
      conversationId,
      selectedByUser
    });
    if (!stableInsertion) {
      if (autoMode) projectAutoStop("inserted_prompt_unverified");
      projectAnnounce("BDB vNext: prompt appeared, but stable composer verification failed; launch left pending.", "warning");
      return projectLaunchResult(false, "project_prompt_inserted_unverified", claimedLaunchId);
    }
    if (autoMode) {
      if (projectCanonicalHandoffSentMatches(canonicalStatus, claimed, conversationId)) {
        projectAutoState = { phase: "sent", launch_id: claimed.launch_id, execution_binding_id: claimed.execution_binding_id, token: "recovered-sent" };
        const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
        if (acknowledged) {
          if (conversationId) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
          return projectLaunchResult(true, "project_prompt_inserted", claimedLaunchId);
        }
        return projectLaunchResult(false, "project_prompt_ack_failed", claimedLaunchId);
      }
      if (projectAutoState.phase === "stopped") {
        return projectLaunchResult(false, "project_auto_stopped", claimedLaunchId);
      }
      const token = `${claimed.launch_id}:${claimed.execution_binding_id}:${Date.now()}`;
      projectAutoState = { phase: "inserting_prompt", launch_id: claimed.launch_id, execution_binding_id: claimed.execution_binding_id, token };
      const sent = await projectAutoSendInserted(claimed, claimId, claimed.prompt, projectFindComposer(), token);
      if (!sent) return projectLaunchResult(false, "project_auto_send_failed", claimedLaunchId);
      const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId, { project_id: claimed.project_id, execution_binding_id: claimed.execution_binding_id, conversation_id: conversationId });
      if (acknowledged) {
        if (conversationId) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
        return projectLaunchResult(true, "project_prompt_inserted", claimedLaunchId);
      }
      return projectLaunchResult(false, "project_prompt_ack_failed", claimedLaunchId);
    }
    const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId);
    if (!acknowledged) {
      projectAnnounce("BDB vNext: prompt is present, but launch ACK failed; do not send yet.", "warning");
      return projectLaunchResult(false, "project_prompt_ack_failed", claimedLaunchId);
    }
    if (conversationId) await projectWriteBinding(claimed, claimId, conversationId, "ACKED");
    projectAnnounce("BDB vNext: project prompt inserted (not sent).", "success");
    return projectLaunchResult(true, "project_prompt_inserted", claimedLaunchId);
  } finally {
    projectInsertionActive = false;
  }
}

async function projectInsertSelectedLaunch() {
  if (!projectPageEligible({ selectedByUser: true })) {
    return { ok: false, code: "conversation_not_eligible" };
  }
  const launch = await projectPeek();
  if (!launch) {
    return { ok: false, code: "no_pending_prompt" };
  }
  return projectHandleLaunch(launch, { selectedByUser: true });
}

async function projectPoll() {
  if (projectPollActive) return;
  projectPollActive = true;
  try {
    const launch = await projectPeek();
    // Manual launches remain owned by the explicit popup action in the tab
    // selected by the user. Only canonical AUTO launches may be consumed by
    // the background poller without a focus heuristic.
    if (launch?.auto_send === true) {
      const handled = await projectHandleLaunch(launch, { automatic: true });
      if (!handled?.ok) return;
    }
  } catch (_error) {
    // A transient Native/DOM failure leaves the canonical launch pending.
  } finally {
    projectPollActive = false;
  }
}

if (typeof chrome === "object" && chrome.runtime && chrome.runtime.onMessage) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === PROJECT_AUTO_STOP_MESSAGE) {
      projectAutoStop("user_stop");
      sendResponse({ ok: true, code: "auto_stopped" });
      return false;
    }
    if (!message || message.type !== PROJECT_LAUNCH_INSERT_MESSAGE) return false;
    projectInsertSelectedLaunch()
      .then(sendResponse)
      .catch(() => sendResponse({ ok: false, code: "project_prompt_not_inserted" }));
    return true;
  });
}

function codeBlocks(root) {
  if (!root) return [];
  const blocks = [];
  if (typeof root.matches === "function" && root.matches("pre code")) {
    blocks.push(root);
  }
  if (typeof root.querySelectorAll === "function") {
    for (const block of root.querySelectorAll("pre code")) {
      if (blocks.length >= MAX_CANONICAL_SWEEP_BLOCKS) break;
      if (!blocks.includes(block)) blocks.push(block);
    }
  }
  return blocks;
}

function isAssistantMessageOwner(element) {
  return Boolean(element && projectMessageAttribute(element, "data-message-author-role") === "assistant");
}

function assistantMessageOwners(root) {
  const owners = new Set();
  const add = (element) => {
    if (projectIsHTMLElement(element) && isAssistantMessageOwner(element)) owners.add(element);
  };
  if (projectIsHTMLElement(root)) {
    add(root);
    if (typeof root.closest === "function") {
      const owner = root.closest("[data-message-author-role='assistant']");
      // closest() has already enforced the semantic selector in a real DOM.
      // Older deterministic DOM fixtures expose the relationship through
      // closest() but do not implement data attributes themselves.
      if (projectIsHTMLElement(owner)) owners.add(owner);
    }
  }
  if (root && typeof root.querySelectorAll === "function") {
    for (const owner of root.querySelectorAll("[data-message-author-role='assistant']")) {
      add(owner);
      if (owners.size >= MAX_CANONICAL_SWEEP_BLOCKS) break;
    }
  }
  // Keep compatibility with rendered legacy pre/code blocks in older DOMs
  // and in minimal DOM implementations that expose only those nodes.
  for (const block of codeBlocks(root)) {
    const owner = assistantOwner(block);
    if (projectIsHTMLElement(owner)) owners.add(owner);
    if (owners.size >= MAX_CANONICAL_SWEEP_BLOCKS) break;
  }
  return Array.from(owners).slice(0, MAX_CANONICAL_SWEEP_BLOCKS);
}

function canonicalSchemaCandidate(text) {
  if (typeof text !== "string" || !text || text.length > MAX_SUBMISSION_TEXT) return false;
  return Boolean(
    parseProjectExecutionResult({ textContent: text }) ||
    parseSubmission({ textContent: text })
  );
}

function canonicalResultCandidates(owner) {
  const candidates = [];
  let visited = 0;
  let exceeded = false;
  const collect = (node, depth = 0) => {
    if (!node || exceeded) return "";
    visited += 1;
    if (visited > MAX_CANONICAL_SCAN_NODES || depth > MAX_CANONICAL_SCAN_DEPTH) {
      exceeded = true;
      return "";
    }
    if (node.nodeType === 3) {
      return typeof node.nodeValue === "string" ? node.nodeValue : node.textContent || "";
    }
    if (node.nodeType !== 1 && !projectIsHTMLElement(node)) return "";
    if (projectIsMessageUiElement(node)) return "";
    if (String(node.tagName || "").toLowerCase() === "br") return "\n";
    const beforeChildren = candidates.length;
    const children = node.childNodes && node.childNodes.length
      ? Array.from(node.childNodes)
      : Array.from(node.children || []);
    let text = "";
    if (children.length) {
      const chunks = [];
      for (const child of children) {
        chunks.push(collect(child, depth + 1));
        if (exceeded) return "";
      }
      text = chunks.join("");
    } else {
      text = typeof node.textContent === "string" ? node.textContent : "";
    }
    if (text.length > MAX_SUBMISSION_TEXT) {
      exceeded = true;
      return "";
    }
    if (candidates.length === beforeChildren && canonicalSchemaCandidate(text)) {
      candidates.push({ block: node, text });
    }
    return text;
  };
  const ownerText = collect(owner);
  if (exceeded || !ownerText || ownerText.length > MAX_SUBMISSION_TEXT || candidates.length === 0) return [];

  // A valid-looking JSON descendant is insufficient if the surrounding
  // assistant message contains prose or other unrelated material. Strip only
  // the exact, complete candidate blocks found in this message and require
  // that nothing except whitespace remains. Multiple adjacent canonical
  // blocks remain supported and are independently deduplicated downstream.
  let remainder = ownerText;
  for (const candidate of candidates) {
    const index = remainder.indexOf(candidate.text);
    if (index < 0) return [];
    remainder = `${remainder.slice(0, index)}${remainder.slice(index + candidate.text.length)}`;
  }
  return remainder.trim() === "" ? candidates : [];
}

function legacyBlockFitsAssistantMessage(block, owner) {
  const text = projectCanonicalMessageText(block);
  if (!canonicalSchemaCandidate(text)) return false;
  const ownerText = projectCanonicalMessageText(owner);
  // Some legacy DOM adapters expose pre/code nodes and their semantic owner
  // separately. In a real connected tree the owner text is present; when it
  // is, no surrounding prose may be discarded to accept the code block.
  if (!ownerText) return true;
  const index = ownerText.indexOf(text);
  if (index < 0) return false;
  const remainder = `${ownerText.slice(0, index)}${ownerText.slice(index + text.length)}`.trim();
  return remainder === "" || canonicalSchemaCandidate(remainder);
}

function scanBlock(block, text) {
  if (!projectIsHTMLElement(block)) return;
  const candidate = { textContent: text };
  const projectResult = parseProjectExecutionResult(candidate);
  if (projectResult) {
    decorateProjectExecution(block, projectResult);
    return;
  }
  const submission = parseSubmission(candidate);
  if (submission) {
    decorate(block, submission);
  }
}

function scan(root = document) {
  const owners = assistantMessageOwners(root);
  for (const owner of owners) {
    for (const candidate of canonicalResultCandidates(owner)) {
      scanBlock(candidate.block, candidate.text);
    }
  }
  for (const block of codeBlocks(root)) {
    const owner = assistantOwner(block);
    if (!projectIsHTMLElement(owner) || !legacyBlockFitsAssistantMessage(block, owner)) continue;
    scanBlock(block, projectCanonicalMessageText(block));
  }
}

function sweepCanonicalResults() {
  if (document.visibilityState !== "visible") {
    return;
  }
  const owners = assistantMessageOwners(document);
  const limit = Math.min(owners.length, MAX_CANONICAL_SWEEP_BLOCKS);
  for (let index = 0; index < limit; index += 1) {
    for (const candidate of canonicalResultCandidates(owners[index])) {
      scanBlock(candidate.block, candidate.text);
    }
  }
  for (const block of codeBlocks(document).slice(0, MAX_CANONICAL_SWEEP_BLOCKS)) {
    const owner = assistantOwner(block);
    if (!projectIsHTMLElement(owner) || !legacyBlockFitsAssistantMessage(block, owner)) continue;
    scanBlock(block, projectCanonicalMessageText(block));
  }
}

function publishRuntimeFingerprint() {
  const root = document && document.documentElement;
  if (root && root.dataset) {
    root.dataset.bdbVnextContentAdapter = CONTENT_ADAPTER_RUNTIME_FINGERPRINT;
  }
}

publishRuntimeFingerprint();
scan(document);
const canonicalSweepTimer = setInterval(sweepCanonicalResults, CANONICAL_RESULT_SWEEP_MS);
if (canonicalSweepTimer && typeof canonicalSweepTimer.unref === "function") canonicalSweepTimer.unref();

const STREAM_SCAN_DEBOUNCE_MS = 50;
const pendingMutationRoots = new Set();
let pendingMutationScan = null;

function mutationScanRoot(node) {
  let element = node;
  if (!element || element.nodeType !== 1) {
    element = element && (element.parentElement || element.parentNode);
  }
  if (!element) return document;
  if (typeof element.closest === "function") {
    return element.closest("[data-message-author-role='assistant']") || element;
  }
  return element;
}

function scheduleMutationScan(node) {
  pendingMutationRoots.add(mutationScanRoot(node));
  if (pendingMutationScan !== null) return;
  pendingMutationScan = setTimeout(() => {
    pendingMutationScan = null;
    const roots = Array.from(pendingMutationRoots);
    pendingMutationRoots.clear();
    for (const root of roots) scan(root);
  }, STREAM_SCAN_DEBOUNCE_MS);
}

scan(document);
const observer = new MutationObserver((records) => {
  for (const record of records) {
    scheduleMutationScan(record.target);
    for (const node of record.addedNodes || []) {
      scheduleMutationScan(node);
    }
  }
});
if (document.documentElement) {
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
}

if (typeof chrome === "object" && chrome.runtime && typeof chrome.runtime.sendMessage === "function") {
  void projectPoll();
  const projectTimer = setInterval(projectPoll, PROJECT_POLL_MS);
  if (projectTimer && typeof projectTimer.unref === "function") projectTimer.unref();
}
