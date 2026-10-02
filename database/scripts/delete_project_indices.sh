#!/usr/bin/env sh
# Old broad deletion was unsafe alongside live and synthetic datasets.
echo "Automatic project-wide deletion is retired. Use explicit owned index names after snapshotting." >&2
exit 2
