# Project Creator allowed-paths hotfix — 0.3.1

## Problem

On Windows/PySide6, constructing `QTextEdit` with the newline-delimited default allowlist caused the text to be interpreted as rich text. The visible defaults were flattened into one space-separated line, so the emitted plan contained one combined allowlist entry instead of separate repository-relative patterns.

## Fix

- construct the widget without initial rich text;
- populate defaults with `setPlainText()`;
- strip empty and whitespace-only lines when building the submitted payload;
- assert exact default-path roundtrip and blank-line normalization in the GUI regression tests.

## Scope

The hotfix changes only the Project Creator dialog and its focused GUI tests. It does not change project creation, GitHub operations, Native Host transport, browser handoff, promotion, push, merge or deployment policy.
