#!/usr/bin/env bash
# Cut a release: bump __version__, commit, tag vX.Y.Z and push both in one go.
#
#   scripts/release.sh [patch|minor|major|X.Y.Z]     (default: patch)
#
# Environment:
#   DRY_RUN=1      show what would happen, change nothing
#   YES=1          no confirmation prompt (CI, scripts)
#   SKIP_TESTS=1   do not run pytest first
#   REMOTE=origin  RELEASE_BRANCH=main
#
# Pushing the tag starts the GitHub workflows: `release` (GitHub release with the
# wheel and sdist) and `docker` (image tagged X.Y.Z and X.Y on ghcr.io).
set -euo pipefail

BUMP="${1:-patch}"
REMOTE="${REMOTE:-origin}"
BRANCH="${RELEASE_BRANCH:-main}"
VERSION_FILE="src/ansreview/__init__.py"
PYTHON="${PYTHON:-python3}"

die() { echo "release: $*" >&2; exit 1; }
run() {
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi
}

cd "$(git rev-parse --show-toplevel)" || die "not inside a git repository"

# ---------------------------------------------------------------- versions
current=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$VERSION_FILE")
[[ $current =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] \
  || die "cannot read a X.Y.Z version from $VERSION_FILE (found '$current')"
maj=${BASH_REMATCH[1]} min=${BASH_REMATCH[2]} pat=${BASH_REMATCH[3]}

case "$BUMP" in
  patch) new="$maj.$min.$((pat + 1))" ;;
  minor) new="$maj.$((min + 1)).0" ;;
  major) new="$((maj + 1)).0.0" ;;
  v[0-9]*|[0-9]*)
    new="${BUMP#v}"
    [[ $new =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must look like 1.2.3, got '$BUMP'"
    ;;
  *) die "usage: $0 [patch|minor|major|X.Y.Z]" ;;
esac

if [ "$(printf '%s\n%s\n' "$current" "$new" | sort -V | tail -n1)" != "$new" ] || [ "$new" = "$current" ]; then
  die "new version $new is not higher than the current $current"
fi
tag="v$new"

# ---------------------------------------------------------------- safety checks
branch=$(git symbolic-ref --quiet --short HEAD || true)
[ "$branch" = "$BRANCH" ] || die "releases are cut from '$BRANCH', you are on '${branch:-a detached HEAD}'"

[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || die "uncommitted changes; commit or stash them first:"$'\n'"$(git status --short --untracked-files=no)"

echo "Fetching $REMOTE…"
git fetch --quiet --tags "$REMOTE" "$BRANCH"
remote_ref="$REMOTE/$BRANCH"
git rev-parse --verify --quiet "refs/remotes/$remote_ref" >/dev/null \
  || remote_ref=FETCH_HEAD
git merge-base --is-ancestor "$remote_ref" HEAD \
  || die "your $BRANCH is behind $REMOTE/$BRANCH; run: git pull --ff-only"

git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$tag" >/dev/null 2>&1 \
  && die "tag $tag already exists on $REMOTE"

ahead=$(git log --oneline "$remote_ref..HEAD")

# ---------------------------------------------------------------- tests
if [ "${SKIP_TESTS:-0}" != 1 ]; then
  echo "Running tests…"
  "$PYTHON" -m pytest -q || die "tests failed; nothing was changed"
fi

# ---------------------------------------------------------------- summary
echo
echo "  version   $current → $new"
echo "  tag       $tag"
echo "  push to   $REMOTE $BRANCH"
if [ -n "$ahead" ]; then
  echo "  also pushes these unpushed commits:"
  while IFS= read -r line; do echo "    $line"; done <<<"$ahead"
fi
echo

if [ "${DRY_RUN:-0}" != 1 ] && [ "${YES:-0}" != 1 ]; then
  [ -t 0 ] || die "not a terminal; set YES=1 to release without confirmation"
  read -r -p "Release $tag? [y/N] " answer
  [[ $answer =~ ^[Yy]([Ee][Ss])?$ ]] || die "cancelled"
fi

# ---------------------------------------------------------------- do it
run sed -i.bak "s/^__version__ = \"$current\"$/__version__ = \"$new\"/" "$VERSION_FILE"
run rm -f "$VERSION_FILE.bak"
if [ "${DRY_RUN:-0}" != 1 ]; then
  grep -q "^__version__ = \"$new\"$" "$VERSION_FILE" || die "could not update $VERSION_FILE"
fi
run git add "$VERSION_FILE"
run git commit --quiet -m "release: $tag"
run git tag -a "$tag" -m "ansreview $new"

if [ "${DRY_RUN:-0}" = 1 ]; then
  echo "  [dry-run] git push --atomic $REMOTE HEAD:$BRANCH $tag"
  echo "Dry run: nothing changed."
  exit 0
fi

# --atomic: the commit and the tag land together or not at all.
if ! git push --atomic "$REMOTE" "HEAD:$BRANCH" "refs/tags/$tag"; then
  git tag -d "$tag" >/dev/null
  git reset --quiet --soft HEAD~1
  git restore --staged "$VERSION_FILE"
  git checkout -- "$VERSION_FILE"
  die "push failed (branch protection? network?); the local commit and tag were undone"
fi

url=$(git remote get-url "$REMOTE" | sed -E 's#^git@([^:]+):#https://\1/#; s#\.git$##; s#^https?://[^@/]+@#https://#')
echo
echo "Released $tag."
case "$url" in
  https://github.com/*)
    echo "  workflows: $url/actions"
    echo "  release:   $url/releases/tag/$tag"
    ;;
esac
