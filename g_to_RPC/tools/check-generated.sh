#!/usr/bin/env bash
# Fails if the committed generated code does not match what the protos and
# the pinned generators produce. Run from the prototype root via
# `make check-generated`, which installs the pinned buf, protoc-gen-go and
# protoc-gen-go-grpc into .tools/bin and puts that first on PATH.
set -euo pipefail

: "${BUF_VERSION:?set by the Makefile}"
: "${PROTOC_GEN_GO_VERSION:?set by the Makefile}"
: "${PROTOC_GEN_GO_GRPC_VERSION:?set by the Makefile}"
: "${UV:=uv}"

fail() { echo "check-generated: $*" >&2; exit 1; }

# Different generator versions produce different code, so a version mismatch
# would show up as drift with a misleading diff. Say so plainly instead.
want() { # want <tool> <got> <wanted>
  [[ "$2" == "$3" ]] || fail "$1 on PATH is ${2:-missing}, want $3 (run \`make clean-tools\`)"
}
want buf "v$(buf --version 2>/dev/null || true)" "$BUF_VERSION"
want protoc-gen-go "$(protoc-gen-go --version 2>/dev/null | awk '{print $2}')" \
  "$PROTOC_GEN_GO_VERSION"
want protoc-gen-go-grpc "$(protoc-gen-go-grpc --version 2>/dev/null | awk '{print "v" $2}')" \
  "$PROTOC_GEN_GO_GRPC_VERSION"

buf lint

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

buf generate -o "$tmp"
diff -ru server/gen "$tmp/server/gen" ||
  fail "server/gen is stale; run \`make generate\` and commit the result"

mkdir -p "$tmp/py"
(cd client && "$UV" run --locked python -m grpc_tools.protoc -I ../proto \
  --python_out="$tmp/py" --grpc_python_out="$tmp/py" \
  --mypy_out="$tmp/py" --mypy_grpc_out="$tmp/py" \
  ecommerce/v1/product.proto) >/dev/null
touch "$tmp/py/ecommerce/__init__.py" "$tmp/py/ecommerce/v1/__init__.py"
diff -ru --exclude=__pycache__ client/ecommerce "$tmp/py/ecommerce" ||
  fail "client/ecommerce is stale; run \`make generate\` and commit the result"

echo "check-generated: buf lint passed and generated code is up to date"
