"use strict";

// ChatGPT can render the next AUTO action while the previous decision is still
// publishing its canonical loop state. A duplicate live panel can also observe
// the same iteration while its durable replay lease is still processing. Retry
// only those exact transient gaps; all replay, iteration, time and opt-in gates
// remain owned by the background worker.
const BDB_AUTO_DECISION_RETRY_ATTEMPTS = 24;
const BDB_AUTO_DECISION_RETRY_MS = 250;
const BDB_AUTO_TRANSIENT_REASONS = new Set([
  "non_sequential_iteration",
  "iteration_in_progress"
]);

const BDB_AUTO_ACTIVE_RUNS = new Map();

function bdbAutoRunKey(action) {
  const automation = action && action.automation;
  if (
    !automation ||
    typeof automation.loop_id !== "string" ||
    !Number.isInteger(automation.iteration)
  ) {
    return null;
  }
  return `${automation.loop_id}:${automation.iteration}`;
}

function bdbAutoPanelDetached(button) {
  if (!button) {
    return true;
  }
  if (button.isConnected === false) {
    return true;
  }

  // Runtime DOM exposes isConnected, but the extension's lightweight browser
  // harnesses intentionally implement only parentElement/classList. This fallback
  // also covers a React-removed panel before all descendant references are cleared.
  const directParent = button.parentElement;
  const isAssistedPanel = Boolean(
    directParent &&
    (
      (
        directParent.classList &&
        directParent.classList.contains("bdb-assisted")
      ) ||
      (
        typeof directParent.className === "string" &&
        directParent.className.split(/\s+/).includes("bdb-assisted")
      )
    )
  );
  return Boolean(
    isAssistedPanel &&
    directParent.parentElement === null
  );
}

function bdbSetAutoButtonText(button, text) {
  if (!button || bdbAutoPanelDetached(button)) {
    return;
  }
  if (button.textContent !== text) {
    button.textContent = text;
  }
}


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
    if (bdbAutoPanelDetached(button)) {
      return {
        executed: false,
        reason: "panel_detached",
        retryCancelled: true
      };
    }

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

    if (attempt === 0 || (attempt + 1) % 4 === 0) {
      bdbSetAutoButtonText(
        button,
        `BDB AUTO: synchronizacja ${latest.expectedIteration}→${iteration}…`
      );
    }
    await bdbAutoDecisionSleep(BDB_AUTO_DECISION_RETRY_MS);
  }

  return latest;
}

async function bdbRunAutoPanel(action, button, output, compact) {
  button.disabled = true;
  bdbSetAutoButtonText(button, "BDB AUTO: sprawdzanie…");
  try {
    const auto = await bdbConsiderAutoWithCatchUp(action, button);
    if (auto && auto.retryCancelled === true) {
      return { retryForReplacement: true };
    }
    if (bdbAutoPanelDetached(button)) {
      return { retryForReplacement: true };
    }
    if (!auto.executed) {
      const suffix = auto.retryExhausted
        ? `${auto.reason || "ASSISTED"}, retry exhausted`
        : (auto.reason || "ASSISTED");
      bdbSetAutoButtonText(button, `BDB: Wykonaj (${suffix})`);
      return { retryForReplacement: false };
    }

    renderResult(output, auto.response, { compact });
    if (auto.resultDelivered === true) {
      bdbSetAutoButtonText(
        button,
        `BDB AUTO: wynik odtworzony (${auto.stopReason || "zakończono"})`
      );
      return { retryForReplacement: false };
    }

    if (bdbAutoPanelDetached(button)) {
      return { retryForReplacement: true };
    }
    const sent = await autoSend(auto.response, auto.loopId, auto.iteration);
    if (sent.sent) {
      try {
        await chrome.runtime.sendMessage({
          type: "BDB_MARK_AUTO_RESULT_DELIVERED",
          loopId: auto.loopId,
          iteration: auto.iteration
        });
      } catch (_error) {
      }
      bdbSetAutoButtonText(
        button,
        auto.shouldContinue
          ? `BDB AUTO: wysłano ${auto.iteration}`
          : `BDB AUTO: wynik wysłany; zatrzymano (${auto.stopReason || "zakończono"})`
      );
      return { retryForReplacement: false };
    }
    bdbSetAutoButtonText(
      button,
      auto.shouldContinue
        ? `BDB AUTO → ASSISTED (${sent.reason})`
        : `BDB AUTO: zatrzymano; wynik oczekuje na ponowienie (${sent.reason})`
    );
    return { retryForReplacement: false };
  } catch (error) {
    if (!bdbAutoPanelDetached(button)) {
      output.textContent = `BDB AUTO error: ${String(error && error.message ? error.message : error)}`;
      bdbSetAutoButtonText(button, "BDB AUTO → ASSISTED");
    }
    return { retryForReplacement: false };
  } finally {
    button.disabled = false;
  }
}

maybeAuto = async function maybeAutoWithDecisionCatchUp(action, button, output, compact) {
  const automation = action && action.automation;
  if (!automation || automation.mode !== "auto") {
    return;
  }

  const runKey = bdbAutoRunKey(action);
  if (runKey) {
    const active = BDB_AUTO_ACTIVE_RUNS.get(runKey);
    if (active && !bdbAutoPanelDetached(active.button)) {
      button.disabled = true;
      bdbSetAutoButtonText(button, "BDB AUTO: oczekiwanie na aktywną operację…");
      try {
        await active.promise;
      } finally {
        button.disabled = false;
      }
      bdbSetAutoButtonText(button, "BDB AUTO: obsłużono w aktywnym panelu");
      return;
    }
  }

  // A live duplicate shares the active promise above. A replacement panel whose
  // previous owner was detached starts immediately, preserving ChatGPT rerender
  // recovery while the background replay guard prevents duplicate execution.
  const promise = bdbRunAutoPanel(action, button, output, compact);
  const entry = { button, promise };
  if (runKey) {
    BDB_AUTO_ACTIVE_RUNS.set(runKey, entry);
  }
  try {
    await promise;
  } finally {
    if (runKey && BDB_AUTO_ACTIVE_RUNS.get(runKey) === entry) {
      BDB_AUTO_ACTIVE_RUNS.delete(runKey);
    }
  }
};
