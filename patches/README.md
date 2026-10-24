# Patch artifacts

Incremental patches for the Cafe AI Replenishment prototype, kept separate from
the application source at the repository root.

| Folder | Transition | Summary |
| --- | --- | --- |
| `1_to_2/` | Step 1 → Step 2 | Weather-aware replenishment: Kuala Lumpur weather and public-holiday context, synthetic historical weather inputs, context-aware backtest. |

## How to apply

From a checkout of the base version (Step 1), at the repository root:

```sh
git apply --check patches/1_to_2/changes.patch
git apply patches/1_to_2/changes.patch
```

Then review the working tree and commit, for example:

```text
feat: add Kuala Lumpur weather and holiday context
```

Each folder contains:

- `changes.patch` — complete Git unified diff for the transition (additions, modifications, deletions).
- `changed_files.txt` — file statuses and paths.
- `DIFF_STAT.txt` — diff statistics.
- `PATCH_SHA256.txt` — checksum for verifying `changes.patch`.
- `README.md` — English change summary and apply instructions.

`1_to_2/` additionally ships a Python-only subset (`python_changes.patch`,
`python_sources/`, `PYTHON_FILES.txt`). Do **not** apply both `changes.patch` and
`python_changes.patch` — they overlap on Python files.

Local password-hash configuration, runtime databases, environment files, private
key containers and Python bytecode/cache folders are excluded from the patches.
