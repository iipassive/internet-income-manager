#!/bin/bash
##############################################################################
# Bump version → commit → tag → push. GitHub Actions then builds the release.
#
# Usage:  bash scripts/release.sh 2.4.4 "release notes message"
##############################################################################
set -e

[ -z "$1" ] && { echo "Usage: $0 <version> [notes]"; exit 1; }
VERSION=$1
NOTES=${2:-Release $VERSION}

# Compute next build = current+1
CUR_BUILD=$(grep -E '^CLIENT_BUILD=' client/app.py | head -1 | sed 's/[^0-9]//g')
NEW_BUILD=$((CUR_BUILD+1))

# Update version constants in client/app.py
sed -i -E "s/^CLIENT_VERSION=\"[^\"]+\"/CLIENT_VERSION=\"$VERSION\"/" client/app.py
sed -i -E "s/^CLIENT_BUILD=[0-9]+/CLIENT_BUILD=$NEW_BUILD/" client/app.py

echo "Bumped: CLIENT_VERSION=$VERSION CLIENT_BUILD=$NEW_BUILD"

git add client/app.py
git commit -m "Release v$VERSION build $NEW_BUILD

$NOTES"
git tag -a "v$VERSION" -m "$NOTES"
git push origin main
git push origin "v$VERSION"

echo
echo "Pushed v$VERSION. GitHub Actions will build iim-client.tar.gz and attach to the release."
echo "Check: https://github.com/iipassive/internet-income-manager/actions"
