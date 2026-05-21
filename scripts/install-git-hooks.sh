#!/bin/sh
# install-git-hooks.sh
#
# Opt-in installer for the wolf attribution-guard git hooks. Copies
# scripts/git-hooks/{pre-commit,commit-msg} into .git/hooks/ in the
# current git repo. Existing hooks are NOT overwritten unless --force is
# given; on first conflict the install aborts without making partial
# changes.
#
# Usage:
#   scripts/install-git-hooks.sh                 # install (abort if hook exists)
#   scripts/install-git-hooks.sh --force         # overwrite existing hooks (backup as .bak)
#   scripts/install-git-hooks.sh --dry-run       # show what would happen, do nothing
#   scripts/install-git-hooks.sh --dry-run --force
#   scripts/install-git-hooks.sh --help
#
# Exit codes:
#   0  success (installed, or dry-run reported plan)
#   1  usage / internal error
#   2  conflict: existing hook present and --force not given

set -eu

EXIT_OK=0
EXIT_ERR=1
EXIT_CONFLICT=2

DRY_RUN=0
FORCE=0

HOOKS="pre-commit commit-msg"

usage() {
    cat >&2 <<'EOF'
usage: scripts/install-git-hooks.sh [--force] [--dry-run] [--help]

Installs the wolf attribution-guard hooks into .git/hooks/.

options:
  --force      overwrite existing hooks (backups the previous file as .bak)
  --dry-run    show planned actions without modifying anything
  --help, -h   show this message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit $EXIT_OK ;;
        *) usage; exit $EXIT_ERR ;;
    esac
done

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "install-git-hooks: not inside a git repository" >&2
    exit $EXIT_ERR
fi

repo_root=$(git rev-parse --show-toplevel)
hooks_dir=$(git rev-parse --git-path hooks)
src_dir="$repo_root/scripts/git-hooks"

# Sanity: source hooks must exist and be regular files.
for h in $HOOKS; do
    src="$src_dir/$h"
    if [ ! -f "$src" ]; then
        echo "install-git-hooks: source hook missing: $src" >&2
        exit $EXIT_ERR
    fi
done

# Phase 1: conflict scan. Abort the whole install if any conflict exists
# and --force is not given.
if [ "$FORCE" -eq 0 ]; then
    for h in $HOOKS; do
        dst="$hooks_dir/$h"
        if [ -e "$dst" ] && [ ! -L "$dst" ]; then
            # Skip if it is literally our own previously-installed hook.
            # We detect this by checksum match.
            src="$src_dir/$h"
            src_sum=$(shasum "$src" | awk '{print $1}')
            dst_sum=$(shasum "$dst" 2>/dev/null | awk '{print $1}')
            if [ "$src_sum" = "$dst_sum" ]; then
                continue
            fi
            echo "install-git-hooks: $dst already exists and differs from ours." >&2
            echo "  re-run with --force to overwrite (backup will be created)." >&2
            exit $EXIT_CONFLICT
        fi
    done
fi

# Phase 2: install (or dry-run).
mkdir_cmd="mkdir -p"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would ensure hooks dir exists: $hooks_dir"
else
    $mkdir_cmd "$hooks_dir"
fi

for h in $HOOKS; do
    src="$src_dir/$h"
    dst="$hooks_dir/$h"
    if [ -e "$dst" ] && [ "$FORCE" -eq 1 ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would back up existing $dst to $dst.bak"
        else
            cp "$dst" "$dst.bak"
        fi
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would install $src -> $dst (chmod +x)"
    else
        cp "$src" "$dst"
        chmod +x "$dst"
    fi
done

if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] complete. No changes made."
else
    echo "install-git-hooks: installed $HOOKS into $hooks_dir"
fi
exit $EXIT_OK
