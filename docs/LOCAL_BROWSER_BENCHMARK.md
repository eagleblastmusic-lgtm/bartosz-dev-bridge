# Local Browser Pilot Performance Benchmark

This runbook measures the preserved synthetic Bartosz Dev Bridge pilot after the functional browser pilot has passed.

## Scope

The automated harness covers:

- Native Host process startup and native-messaging request/response;
- Direct Lane durable submission and ingestion;
- Bridge validation and scheduler latency;
- isolated session worktree preparation;
- `open_read` execution;
- `multi_file_patch` plus the `poc_pytest` profile;
- result staging and publication;
- cold Bridge startup followed by the first read.

The harness does not measure ChatGPT answer generation, DOM streaming, human reaction time, the click itself, or browser rendering. The successful assisted browser pilot remains the functional proof for that outer UI layer.

## Safety requirements

The command refuses to run unless all of the following are true:

- Bridge begins `OFFLINE`;
- the repository id is exactly `bdb-local-browser-pilot`;
- Native Host exposes exactly the `pilot` alias;
- the allowlist is exactly `src/clamp.py`, `tests/test_clamp.py`, and `PILOT_RESULT.md`;
- both the Bridge checkout and synthetic fixture checkout are clean;
- the preserved read and patch actions still match their expected schemas.

Every operation creates a fresh session. Mutation runs therefore use isolated worktrees and do not change the source fixture checkout. The harness never deletes a worktree, Journal, result, configuration, Native Host registration, or previous pilot artifact.

## Standard series

Run from the clean `main` checkout after the benchmark PR has been merged and fast-forwarded locally:

```powershell
$ErrorActionPreference = "Stop"
Set-Location "C:\Projekty\DevMaster\bartosz-dev-bridge"

$python = ".\.venv\Scripts\python.exe"
$root = Join-Path $env:LOCALAPPDATA `
    "BartoszDevBridge\local-browser-pilot"

& $python .\scripts\run_local_browser_benchmark.py `
    --root $root `
    --open-read-runs 20 `
    --patch-runs 10 `
    --cold-start-runs 5 `
    --operation-timeout-seconds 60

if ($LASTEXITCODE -ne 0) {
    throw "Benchmark zakończył się błędem funkcjonalnym."
}
```

Do not open or click an old browser action while the automated series is running.

## Output

Each run creates a new preserved directory:

```text
%LOCALAPPDATA%\BartoszDevBridge\local-browser-pilot\benchmarks\<UTC timestamp>\
```

It contains:

- `benchmark.json` — every raw run, command id, timing breakdown, summary, thresholds and environment;
- `benchmark.md` — compact operator report.

The report uses the nearest-rank percentile method and includes minimum, mean, p50, p95 and maximum.

## Initial targets

| Suite | p50 target | p95 target |
|---|---:|---:|
| warm `open_read` native roundtrip | ≤ 1 s | ≤ 2 s |
| warm `multi_file_patch + pytest` native roundtrip | ≤ 3 s | ≤ 5 s |
| cold start through completed first read | ≤ 5 s | ≤ 10 s |
| result publication across successful runs | ≤ 0.5 s | ≤ 1 s |

A missed performance target is reported as `target_miss` but does not by default turn the process into a functional failure. Add `--fail-on-target-miss` when the target table should act as a strict gate.

## End state

The harness disarms Native Host and returns Bridge to `OFFLINE`, including after an individual run fails. It then rechecks that the source fixture checkout remains clean. All benchmark worktrees, Journal rows and result files stay preserved for audit and later analysis.
