# Step 3 → Step 4: Final demo package and verification

**Base:** Folder 3 (`Cafe_AI_Replenishment_v3_6_1_Connected_20261023`)  
**Target:** Folder 4 (`Cafe_AI_Replenishment_v3_6_2_Final_Verified_20261023`)

Consolidates the local workbench and public-site package with startup/demo guides and dated verification records. The documented demo covers sale, undo, stock alerts, quotes, approval and downloadable drafts. API acceptance, user-confirmed receipt and provider-confirmed delivery remain distinct states.

## Apply

From the repository root at the matching base version, run:

```sh
git apply --check changes.patch
git apply changes.patch
```

Review the working tree, then commit with a message such as:

```text
feat: finalize the demo workflow and verification package
```

`changes.patch` is a complete Git binary-capable unified diff for this transition. It includes additions, modifications and deletions required to reach the next snapshot. `changed_files.txt` lists statuses and paths. The patch was generated from the supplied adjacent folders and passed `git apply --check` against its filtered base snapshot.

## Submission hygiene

Local password-hash configuration (`auth_config.json`), Python bytecode/cache folders, runtime databases, environment files and private key containers are excluded. No project tests were run.


## Python source files for this transition

Python file changes: 2 added, 0 modified, 2 deleted. Full target versions of the added and modified Python files are in `python_sources/`; `PYTHON_FILES.txt` lists their statuses. `python_changes.patch` contains only the Python-file diff and passed `git apply --check`. For the complete project transition, apply `changes.patch` instead; do not apply both patches because they overlap on Python files.
