#!/bin/sh
# install-uv.sh: install the pinned uv release into ~/.local/bin from its release tarball, sha256-checked
# (never curl | sh). Idempotent: exits 0 at once when ~/.local/bin/uv already reports the pinned version.
# Then installs the managed CPython 3.13 that `assistant ci` tests against (3.11 = the system python3).
#   install-uv.sh [--check]   --check: only report whether the pinned uv is installed (exit 0 yes, 1 no)
# Env: UV_INSTALL_DIR (default ~/.local/bin). The pin is bumped by editing VERSION + SHA256 below
# (sha256 from the release's .sha256 asset AND the GitHub API digest; both must agree).
set -eu
VERSION=0.12.23
ASSET=uv-x86_64-unknown-linux-gnu.tar.gz
SHA256=9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6
dest=${UV_INSTALL_DIR:-$HOME/.local/bin}
have() { [ -x "$dest/uv" ] && "$dest/uv" --version 2>/dev/null | grep -q "^uv $VERSION\b"; }
if [ "${1:-}" = --check ]; then have; exit; fi
if ! have; then
  [ "$(uname -m)" = x86_64 ] || { echo "install-uv: only x86_64 is pinned" >&2; exit 2; }
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/install-uv.XXXXXX")
  trap 'rm -rf "${tmp:?}"' EXIT
  curl -fsSL --retry 3 --max-time 120 -o "$tmp/$ASSET" "https://github.com/astral-sh/uv/releases/download/$VERSION/$ASSET"
  echo "$SHA256  $tmp/$ASSET" | sha256sum -c --quiet - || { echo "install-uv: sha256 mismatch, not installed" >&2; exit 1; }
  tar -xzf "$tmp/$ASSET" -C "$tmp"
  mkdir -p "$dest"
  for b in uv uvx; do
    install -m 0755 "$tmp/${ASSET%.tar.gz}/$b" "$dest/.$b.new" && mv -f "$dest/.$b.new" "$dest/$b"
  done
  have || { echo "install-uv: installed binary does not report $VERSION" >&2; exit 1; }
fi
"$dest/uv" python install 3.13 >/dev/null 2>&1 || "$dest/uv" python install 3.13
"$dest/uv" --version
