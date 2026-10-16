#!/usr/bin/env bash
# decompdecomp's dev branch, for testing what isn't released yet (Linux):
#
#   curl -fsSL https://raw.githubusercontent.com/n-delic/decomp.wtf/main/site/public/dev.sh | bash
#
# Runs the dev branch's own installer (install.sh on GitHub) with DECOMP_BRANCH=dev: decompdecomp installs from the
# branch's newest commit, and the app then offers every new commit pushed there as an update (one click).
# Back to releases: curl -fsSL https://raw.githubusercontent.com/n-delic/decomp.wtf/main/site/public/install.sh | bash
set -euo pipefail
BRANCH="${DECOMP_BRANCH:-dev}"
curl -fsSL "https://raw.githubusercontent.com/n-delic/decomp.wtf/$BRANCH/site/public/install.sh" | DECOMP_BRANCH="$BRANCH" bash -s -- "$@"
