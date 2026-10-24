# Step 4 → Step 5: v3.7 published release

**Base:** Folder 4 (`Cafe_AI_Replenishment_v3_6_2_Final_Verified_20261023`)  
**Target:** Folder 5 (`Cafe_AI_Replenishment_v3_7_Final_Published_Apache2_0_20261024`)

Adds the v3.7 release with weighted recent-sales history, custom purchase lines, updated local startup/password setup, public account workflow, cross-platform compatibility fixes and a dated public acceptance report. The release documents Apache-2.0 scope and distinguishes historical checks from this release’s acceptance evidence.

## Apply

From the repository root at the matching base version, run:

```sh
git apply --check changes.patch
git apply changes.patch
```

Review the working tree, then commit with a message such as:

```text
feat: publish v3.7 with sales history and custom purchases
```

`changes.patch` is a complete Git binary-capable unified diff for this transition. It includes additions, modifications and deletions required to reach the next snapshot. `changed_files.txt` lists statuses and paths. The patch was generated from the supplied adjacent folders and passed `git apply --check` against its filtered base snapshot.

## Submission hygiene

Local password-hash configuration (`auth_config.json`), Python bytecode/cache folders, runtime databases, environment files and private key containers are excluded. No project tests were run.


## Python source files for this transition

Python file changes: 8 added, 7 modified, 6 deleted. Full target versions of the added and modified Python files are in `python_sources/`; `PYTHON_FILES.txt` lists their statuses. `python_changes.patch` contains only the Python-file diff and passed `git apply --check`. For the complete project transition, apply `changes.patch` instead; do not apply both patches because they overlap on Python files.
