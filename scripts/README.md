# scripts — by job

| Chunk | Job | Entry |
|-------|-----|--------|
| **remote-superset/** | Local → DO content merge (union, no delete) | `Sync-RemoteSuperset.sh` |

```bash
# Git Bash
cd scripts/remote-superset
./Sync-RemoteSuperset.sh --dry-run
./Sync-RemoteSuperset.sh
```

Runtime stash: `remote-superset/_from-remote/` (gitignored).
