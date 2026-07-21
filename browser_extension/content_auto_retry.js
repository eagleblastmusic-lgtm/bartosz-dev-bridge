"use strict";

// ChatGPT can render the next AUTO action while the previous decision is still
// publishing its canonical loop state. A duplicate live panel can also observe
// the same iteration while its durable replay lease is still processing. Retry
// only those exact transient gaps; all replay, iteration, time and opt-in gates
// remain owned by the background worker.
const BDB_AUTO_DECISION_RETRY_ATTEMPTS = 240;
const BDB_AUTO_DECISION_RETRY_MS = 250;
const BDB_AUTO_TRANSIENT_REASONS = new Set([
  "non_sequential_iteration",
  "iteration_in_progress"
]);

function bdbAutoDecisionSleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bdbAutoActionIteration(action) {
  const automation = action && action.automation;
  return automation && Number.isInteger(automation.iteration)
    ? automation.iteration
    : null;
}

function bdbAutoDecisionNeedsCatchUp(auto, iteration) {
  return Boolean(
    auto &&
    auto.executed === false &&
    BDB_AUTO_TRANSIENT_REASONS.has(auto.reason) &&
    Number.isInteger(iteration) &&
    Number.isInteger(auto.expectedIteration) &&
    auto.expectedIteration <= iteration
  );
}

async function bdbConsiderAutoWithCatchUp(action, button) {
  const iteration = bdbAutoActionIteration(action);
  let latest = null;

  for (let attempt = 0; attempt < BDB_AUTO_DECISION_RETRY_ATTEMPTS; attempt += 1) {
    const decision = await chrome.runtime.sendMessage({ type: "BDB_CONSIDER_AUTO", action });
    if (!decision || decision.ok !== true) {
      throw new Error(decision && decision.error ? decision.error : "Brak decyzji AUTO");
    }

    latest = decision.response;
    if (!bdbAutoDecisionNeedsCatchUp(latest, iteration)) {
      return latest;
    }

    if (attempt + 1 >= BDB_AUTO_DECISION_RETRY_ATTEMPTS) {
      return { ...latest, retryExhausted: true };
    }

    button.textContent = `BDB AUTO: synchronizacja ${latest.expectedIteration}→${iteration}…`;
    await bdbAutoDecisionSleep(BDB_AUTO_DECISION_RETRY_MS);
  }

  return latest;
}

maybeAuto = async function maybeAutoWithDecisionCatchUp(action, button, output, compact) {
  const automation = action && action.automation;
  if (!automation || automation.mode !== "auto") {
    return;
  }

  button.disabled = true;
  button.textContent = "BDB AUTO: sprawdzanie…";
  try {
    const auto = await bdbConsiderAutoWithCatchUp(action, button);
    if (!auto.executed) {
      const suffix = auto.retryExhausted ? `${auto.reason || "ASSISTED"}, retry exhausted` : (auto.reason || "ASSISTED");
      button.textContent = `BDB: Wykonaj (${suffix})`;
      return;
    }

    renderResult(output, auto.response, { compact });
    if (!auto.shouldContinue) {
      button.textContent = `BDB AUTO: zatrzymano (${auto.stopReason || "limit"})`;
      return;
    }

    const sent = await autoSend(auto.response, auto.loopId, auto.iteration);
    if (sent.sent) {
      button.textContent = `BDB AUTO: wysłano ${auto.iteration}`;
      return;
    }
    button.textContent = `BDB AUTO → ASSISTED (${sent.reason})`;
  } catch (error) {
    output.textContent = `BDB AUTO error: ${String(error && error.message ? error.message : error)}`;
    button.textContent = "BDB AUTO → ASSISTED";
  } finally {
    button.disabled = false;
  }
};
