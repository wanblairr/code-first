# Step 1 → Step 2: Weather-aware replenishment

**Base:** Folder 1 (`Cafe_AI_Replenishment_v3`)  
**Target:** Folder 2 (`Cafe_AI_Replenishment_v3_2_final`)

Adds Kuala Lumpur weather and public-holiday context, synthetic historical weather inputs and a documented context-aware backtest. Startup prepares demo data, refreshes weather when available, calculates a proposal and starts the mobile workbench. Documentation clarifies synthetic data, password access, date handling and operational limits.

## Apply

From the repository root at the matching base version, run:

```sh
git apply --check changes.patch
git apply changes.patch
```

Review the working tree, then commit with a message such as:

```text
feat: add Kuala Lumpur weather and holiday context
```

`changes.patch` is a complete Git binary-capable unified diff for this transition. It includes additions, modifications and deletions required to reach the next snapshot. `changed_files.txt` lists statuses and paths. The patch was generated from the supplied adjacent folders and passed `git apply --check` against its filtered base snapshot.

## Submission hygiene

Local password-hash configuration (`auth_config.json`), Python bytecode/cache folders, runtime databases, environment files and private key containers are excluded. No project tests were run.


## Python source files for this transition

Python file changes: 14 added, 8 modified, 0 deleted. Full target versions of the added and modified Python files are in `python_sources/`; `PYTHON_FILES.txt` lists their statuses. `python_changes.patch` contains only the Python-file diff and passed `git apply --check`. For the complete project transition, apply `changes.patch` instead; do not apply both patches because they overlap on Python files.
