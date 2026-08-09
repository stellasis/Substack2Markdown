# scripts — automation / helpers

| Job | Entry |
|-----|--------|
| Local → DO content merge (union, no delete) | `./Sync-RemoteSuperset.sh` |

```bash
# Git Bash
./scripts/Sync-RemoteSuperset.sh --dry-run
./scripts/Sync-RemoteSuperset.sh
```

Runtime stash (remote-only pulls): `.substack-sync/_from-remote/` (gitignored).
