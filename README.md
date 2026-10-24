# Step 2 → Step 3: BOM inventory and purchase review

**Base:** Folder 2 (`Cafe_AI_Replenishment_v3_2_final`)  
**Target:** Folder 3 (`Cafe_AI_Replenishment_v3_6_1_Connected_20261023`)

Adds recipe/BOM inventory handling, safety-stock warnings, guarded undo of the latest simulated sale, package pricing in MYR, quantity review, approval snapshots and supplier-ready drafts. The local workbench update is distinguished from public-site deployment; optional Gmail, WhatsApp and Runpod flows retain explicit user confirmation.

## Apply

From the repository root at the matching base version, run:

```sh
git apply --check changes.patch
git apply changes.patch
```

Review the working tree, then commit with a message such as:

```text
feat: add BOM inventory controls and reviewed purchase drafts
```

`changes.patch` is a complete Git binary-capable unified diff for this transition. It includes additions, modifications and deletions required to reach the next snapshot. `changed_files.txt` lists statuses and paths. The patch was generated from the supplied adjacent folders and passed `git apply --check` against its filtered base snapshot.

## Submission hygiene

Local password-hash configuration (`auth_config.json`), Python bytecode/cache folders, runtime databases, environment files and private key containers are excluded. No project tests were run.


## Python source files for this transition

Python file changes: 270 added, 0 modified, 225 deleted. Full target versions of the added and modified Python files are in `python_sources/`; `PYTHON_FILES.txt` lists their statuses. `python_changes.patch` contains only the Python-file diff and passed `git apply --check`. For the complete project transition, apply `changes.patch` instead; do not apply both patches because they overlap on Python files.
