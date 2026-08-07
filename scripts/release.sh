#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
cd "$ROOT"

VERSION_VALUE=${1#v}
if [[ ! "$VERSION_VALUE" =~ '^[0-9]+\.[0-9]+\.[0-9]+$' ]]; then
  echo "使い方: scripts/release.sh <X.Y.Z>" >&2
  exit 2
fi

TAG="v${VERSION_VALUE}"
CURRENT_VERSION=$(tr -d '[:space:]' < VERSION)
if [[ "$CURRENT_VERSION" != "$VERSION_VALUE" ]]; then
  echo "VERSION は ${CURRENT_VERSION} です。${VERSION_VALUE} と一致しません" >&2
  exit 1
fi
if [[ $(git branch --show-current) != "main" ]]; then
  echo "main ブランチで実行してください" >&2
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "未コミットの変更があります。先にcommitしてください" >&2
  exit 1
fi

echo "lintを実行します"
ruff check server.py test_server.py
echo "testを実行します"
python3 -m unittest -v

git fetch origin main --tags
if ! git merge-base --is-ancestor origin/main HEAD; then
  echo "origin/main にローカルへ未反映の変更があります" >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  if [[ $(git rev-list -n 1 "$TAG") != $(git rev-parse HEAD) ]]; then
    echo "${TAG} は別のcommitを指しています" >&2
    exit 1
  fi
else
  git tag -a "$TAG" -m "Agent Deck ${TAG}"
fi

git push origin main
git push origin "$TAG"
if gh release view "$TAG" >/dev/null 2>&1; then
  echo "GitHub Release ${TAG} は公開済みです"
else
  gh release create "$TAG" --title "Agent Deck ${TAG}" --generate-notes
fi
