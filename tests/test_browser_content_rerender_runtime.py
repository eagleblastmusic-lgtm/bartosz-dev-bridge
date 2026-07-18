from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_content_script_restores_panel_when_chatgpt_removes_panel_but_keeps_code_node(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser content-script runtime contract")

    harness = tmp_path / "content-rerender-runtime.cjs"
    harness.write_text(
        textwrap.dedent(
            r"""
            "use strict";

            const assert = require("node:assert/strict");
            const fs = require("node:fs");
            const path = require("node:path");
            const vm = require("node:vm");

            const extensionDir = process.argv[2];

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
                this.disabled = false;
                this.type = "";
              }
              matches(selector) {
                return selector === "code" && this.tagName === "CODE";
              }
              append(...nodes) {
                for (const node of nodes) {
                  node.parentElement = this;
                  this.children.push(node);
                }
              }
              removeChild(node) {
                const index = this.children.indexOf(node);
                assert.notEqual(index, -1);
                this.children.splice(index, 1);
                node.parentElement = null;
              }
              addEventListener() {}
              closest(selector) {
                let current = this;
                while (current) {
                  if (selector === "pre" && current.tagName === "PRE") return current;
                  if (selector === "form" && current.tagName === "FORM") return current;
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
                return null;
              }
              querySelectorAll(selector) {
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

            let sendCalls = 0;
            let observerCallback = null;
            class FakeMutationObserver {
              constructor(callback) {
                observerCallback = callback;
              }
              observe() {}
            }

            const document = new FakeElement("document");
            document.documentElement = document;
            document.createElement = (tagName) => (
              tagName === "button" ? new FakeButton() : new FakeElement(tagName)
            );
            document.querySelector = () => null;

            const context = {
              console,
              document,
              navigator: {},
              window: { getSelection: () => null },
              HTMLElement: FakeElement,
              HTMLButtonElement: FakeButton,
              HTMLInputElement: FakeInput,
              HTMLTextAreaElement: FakeTextArea,
              InputEvent: FakeInputEvent,
              MutationObserver: FakeMutationObserver,
              setTimeout,
              clearTimeout,
              chrome: {
                runtime: {
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

            const scriptPath = path.join(extensionDir, "content.js");
            vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, {
              filename: scriptPath
            });
            assert.equal(typeof context.scan, "function");
            assert.equal(typeof observerCallback, "function");

            const host = new FakeElement("pre");
            const code = new FakeElement("code");
            code.textContent = JSON.stringify({
              schema: "bdb-action-v1",
              repo_alias: "calculator",
              operation: "workspace_context",
              payload: {},
              automation: {
                mode: "auto",
                loop_id: "rerender-runtime-loop",
                iteration: 1
              },
              presentation: { mode: "compact" }
            });
            host.append(code);

            context.scan(host);
            const firstPanel = host.querySelector(":scope > .bdb-assisted");
            assert.ok(firstPanel, "first scan should attach the BDB panel");
            assert.equal(sendCalls, 1);

            // ChatGPT/React may reconcile the assistant message and remove extension-owned
            // children while preserving the same <code> node and its identity.
            host.removeChild(firstPanel);
            assert.equal(host.querySelector(":scope > .bdb-assisted"), null);

            context.scan(host);
            assert.ok(
              host.querySelector(":scope > .bdb-assisted"),
              "a later scan must restore the panel even when the code node is unchanged"
            );
            assert.equal(sendCalls, 2, "restoration should reconsider AUTO through the replay guard");
            """
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
