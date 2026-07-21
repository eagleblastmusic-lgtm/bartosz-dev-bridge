from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_content_script_runs_delayed_document_scan_after_streaming_settles(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "content-settle-reconciliation-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r'''
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];
            const manifest = JSON.parse(
              fs.readFileSync(path.join(extensionDir, "manifest.json"), "utf8")
            );

            class FakeClassList {
              constructor() {
                this.values = new Set();
              }
              add(...names) {
                for (const name of names) this.values.add(name);
              }
              contains(name) {
                return this.values.has(name);
              }
            }

            class FakeElement {
              constructor(tagName = "div") {
                this.tagName = String(tagName).toUpperCase();
                this.children = [];
                this.parentElement = null;
                this.textContent = "";
                this.className = "";
                this.classList = new FakeClassList();
                this.dataset = {};
                this.disabled = false;
                this.type = "";
              }
              matches(selector) {
                if (selector === ".bdb-output") {
                  return this.className.split(/\s+/).includes("bdb-output");
                }
                if (selector === "code") return this.tagName === "CODE";
                if (selector === "pre") return this.tagName === "PRE";
                if (selector === "code, pre" || selector === "pre, code") {
                  return this.tagName === "CODE" || this.tagName === "PRE";
                }
                return false;
              }
              append(...nodes) {
                for (const node of nodes) {
                  node.parentElement = this;
                  this.children.push(node);
                }
              }
              addEventListener() {}
              closest(selector) {
                let current = this;
                while (current) {
                  if (selector === "pre" && current.tagName === "PRE") return current;
                  if (selector === "form" && current.tagName === "FORM") return current;
                  if (selector === ".bdb-compact" && current.className.split(/\s+/).includes("bdb-compact")) return current;
                  if ((selector === "pre, code" || selector === "code, pre") &&
                      (current.tagName === "PRE" || current.tagName === "CODE")) {
                    return current;
                  }
                  current = current.parentElement;
                }
                return null;
              }
              querySelector(selector) {
                if (selector === ":scope > .bdb-assisted") {
                  return this.children.find(
                    (child) => child.className.split(/\s+/).includes("bdb-assisted")
                  ) || null;
                }
                if (selector === ".bdb-assisted") {
                  let found = null;
                  const visit = (node) => {
                    for (const child of node.children) {
                      if (child.className.split(/\s+/).includes("bdb-assisted")) {
                        found = child;
                        return;
                      }
                      visit(child);
                      if (found) return;
                    }
                  };
                  visit(this);
                  return found;
                }
                if (selector === "pre code, code") {
                  return this.querySelectorAll(selector)[0] || null;
                }
                if (selector === "code") {
                  return this.children.find((child) => child.tagName === "CODE") || null;
                }
                if (selector === ".bdb-result" || selector === ".bdb-controls") {
                  return this.children.find(
                    (child) => child.className.split(/\s+/).includes(selector.slice(1))
                  ) || null;
                }
                return null;
              }
              querySelectorAll(selector) {
                if (selector === ".bdb-output") {
                  const outputs = [];
                  const visitOutputs = (node) => {
                    for (const child of node.children) {
                      if (child.className.split(/\s+/).includes("bdb-output")) outputs.push(child);
                      visitOutputs(child);
                    }
                  };
                  visitOutputs(this);
                  return outputs;
                }
                if (selector !== "pre code, code") return [];
                const found = [];
                const visit = (node) => {
                  for (const child of node.children) {
                    if (child.tagName === "CODE") found.push(child);
                    visit(child);
                  }
                };
                visit(this);
                return found;
              }
            }

            class FakeButton extends FakeElement {
              constructor() {
                super("button");
              }
              click() {}
            }

            class FakeInput extends FakeElement {}
            class FakeTextArea extends FakeElement {}
            class FakeInputEvent {}

            const observerCallbacks = [];
            class FakeMutationObserver {
              constructor(callback) {
                observerCallbacks.push(callback);
              }
              observe() {}
            }

            const scheduledTimers = [];
            function fakeSetTimeout(callback) {
              scheduledTimers.push(callback);
              return scheduledTimers.length;
            }

            let sendCalls = 0;
            const document = new FakeElement("document");
            document.documentElement = document;
            document.visibilityState = "visible";
            document.hasFocus = () => true;
            document.createElement = (tagName) => (
              tagName === "button" ? new FakeButton() : new FakeElement(tagName)
            );
            document.querySelector = () => null;

            const localStore = {};
            const context = {
              console,
              document,
              location: { pathname: "/c/test-conversation-12345678" },
              navigator: {},
              window: { getSelection: () => null },
              HTMLElement: FakeElement,
              HTMLButtonElement: FakeButton,
              HTMLInputElement: FakeInput,
              HTMLTextAreaElement: FakeTextArea,
              InputEvent: FakeInputEvent,
              MutationObserver: FakeMutationObserver,
              setTimeout: fakeSetTimeout,
              clearTimeout() {},
              chrome: {
                storage: {
                  local: {
                    async get(key) {
                      if (typeof key === "string" && Object.prototype.hasOwnProperty.call(localStore, key)) {
                        return { [key]: JSON.parse(JSON.stringify(localStore[key])) };
                      }
                      return {};
                    },
                    async set(values) {
                      Object.assign(localStore, JSON.parse(JSON.stringify(values)));
                    }
                  }
                },
                runtime: {
                  id: "",
                  async sendMessage(message) {
                    assert.equal(message.type, "BDB_CONSIDER_AUTO");
                    sendCalls += 1;
                    return {
                      ok: true,
                      response: {
                        executed: false,
                        reason: "test_assisted_fallback"
                      }
                    };
                  }
                }
              }
            };
            context.globalThis = context;
            vm.createContext(context);

            let reconciliationObserver = null;
            for (const scriptName of manifest.content_scripts[0].js) {
              const scriptPath = path.join(extensionDir, scriptName);
              vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, {
                filename: scriptPath
              });
              if (scriptName === "content_rerender.js") {
                reconciliationObserver = observerCallbacks.at(-1);
              }
            }

            const host = new FakeElement("pre");
            const code = new FakeElement("code");
            code.textContent = '{"schema":"bdb-action-v1"';
            host.append(code);
            document.append(host);

            context.scan(host);
            assert.equal(
              host.querySelector(":scope > .bdb-assisted"),
              null,
              "partial streaming JSON must not be enhanced"
            );

            assert.equal(typeof reconciliationObserver, "function");
            reconciliationObserver([
              {
                type: "characterData",
                target: { parentElement: code },
                addedNodes: [],
                removedNodes: []
              }
            ]);
            assert.ok(
              scheduledTimers.length > 0,
              "a relevant streaming mutation must schedule delayed document reconciliation"
            );

            code.textContent = JSON.stringify({
              schema: "bdb-action-v1",
              repo_alias: "calculator",
              operation: "workspace_context",
              payload: {},
              automation: {
                mode: "auto",
                loop_id: "settle-runtime-loop",
                iteration: 2
              },
              presentation: { mode: "compact" }
            });

            while (scheduledTimers.length > 0) {
              scheduledTimers.shift()();
            }

            assert.ok(
              host.querySelector(":scope > .bdb-assisted"),
              "the delayed global scan must enhance JSON that became valid after the mutation"
            );
            assert.equal(sendCalls, 1);
            '''
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [node, str(harness), str(EXTENSION)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
