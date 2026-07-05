#!/usr/bin/env bash
# bump_and_release.sh — one-shot version bump + tag + push.
#
# Usage:
#   ./scripts/bump_and_release.sh 0.3.0
#   ./scripts/bump_and_release.sh 0.3.0 --dry-run
#
# What it does:
#   1. Validates the new version (X.Y.Z, no leading "v", no garbage).
#   2. Updates version in pyproject.toml.
#   3. Adds a "## [X.Y.Z] — $(date)" section to CHANGELOG.md if not
#      already present (the existing [Unreleased] section gets renamed).
#   4. Commits both files on the current branch.
#   5. Creates an annotated tag vX.Y.Z.
#   6. Pushes the commit AND the tag to origin.
#
# Then GitHub Actions picks up the tag → release.yml runs → PyPI trusted
# publish completes within ~2 min.
#
# Pre-requisites:
#   - Working tree is clean (`git status` shows no unstaged changes).
#   - You're on the branch you want to release from (usually `main`).
#   - PyPI Trusted Publisher is already configured for this repo +
#     workflow path (one-time setup; see docs/publishing.md).
#   - You have push access to origin.

set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "==> $*"; }

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; shift; fi
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,28p' "$0"
    exit 0
fi
NEW_VERSION="${1:-}"
[ -n "$NEW_VERSION" ] || die "Usage: $0 <X.Y.Z> [--dry-run]"

if ! [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "Version must look like X.Y.Z (e.g. 0.3.0); got '$NEW_VERSION'"
fi

# Working tree must be clean so the version bump commit is atomic.
if ! git diff --quiet HEAD 2>/dev/null; then
    die "Working tree has unstaged changes. Commit or stash them first."
fi

# Resolve repo root (script lives in scripts/).
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

PYPROJECT="$ROOT/pyproject.toml"
CHANGELOG="$ROOT/CHANGELOG.md"

# Current version from pyproject.toml.
CUR_VERSION="$(uv run --no-project python -c "
import tomllib, pathlib
print(tomllib.loads(pathlib.Path('$PYPROJECT').read_text(encoding='utf-8'))['project']['version'])
")"
log "Current version: $CUR_VERSION"
log "New version:     $NEW_VERSION"

if [ "$CUR_VERSION" = "$NEW_VERSION" ]; then
    die "New version equals current version; nothing to do."
fi

# --- 1. Update pyproject.toml -------------------------------------------------
log "Updating $PYPROJECT"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would set version = \"$NEW_VERSION\""
else
    # `tomllib` is read-only; we use a tiny in-place rewrite that
    # only touches the project.version line.
    python - <<PY
import re, pathlib
p = pathlib.Path("$PYPROJECT")
src = p.read_text(encoding="utf-8")
new = re.sub(r'(?m)^version\s*=\s*"[^"]+"', 'version = "$NEW_VERSION"', src, count=1)
if new == src:
    raise SystemExit("Could not find version= in $PYPROJECT")
p.write_text(new, encoding="utf-8")
PY
fi

# --- 2. Update CHANGELOG.md ---------------------------------------------------
log "Updating $CHANGELOG"
TODAY="$(date -u +%Y-%m-%d)"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would promote [Unreleased] → [${NEW_VERSION}] — ${TODAY}"
else
python - <<PY
import re, pathlib
p = pathlib.Path("$CHANGELOG")
src = p.read_text(encoding="utf-8")
# Promote the current [Unreleased] header to a dated versioned section,
# then re-add an empty [Unreleased] section above so the next round of
# work has somewhere to land.
new = re.sub(
    r"## \[Unreleased\]\n",
    "## [Unreleased]\n\n## [$NEW_VERSION] — $TODAY\n",
    src,
    count=1,
)
# Also append a comparison link for the new tag at the bottom.
new = new.rstrip() + (
    "\n[$NEW_VERSION]: https://github.com/bilbilmyc/k8s-mcp/compare/v${CUR_VERSION}...v$NEW_VERSION\n"
)
p.write_text(new, encoding="utf-8")
PY
fi

# --- 3. Commit ----------------------------------------------------------------
COMMIT_MSG="Release $NEW_VERSION"
log "Committing: $COMMIT_MSG"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would commit pyproject.toml + CHANGELOG.md"
else
    git add "$PYPROJECT" "$CHANGELOG"
    git commit -m "$COMMIT_MSG"
fi

# --- 4. Tag -------------------------------------------------------------------
TAG="v$NEW_VERSION"
log "Tagging: $TAG"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would create annotated tag $TAG"
else
    git tag -a "$TAG" -m "Release $NEW_VERSION"
fi

# --- 5. Push ------------------------------------------------------------------
log "Pushing branch + tag to origin"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would git push origin HEAD && git push origin $TAG"
else
    git push origin HEAD
    git push origin "$TAG"
fi

cat <<EOF

Done. GitHub Actions will now:
  1. Run release.yml on tag $TAG
  2. Build sdist + wheel with uv
  3. Publish to PyPI via OIDC trusted publishing
 4. Verify the upload at https://pypi.org/project/k8s-mcp-bilbilmyc/$NEW_VERSION/

Watch progress: https://github.com/bilbilmyc/k8s-mcp/actions/workflows/release.yml
EOF