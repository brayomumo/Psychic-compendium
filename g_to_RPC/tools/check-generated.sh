#!/usr/bin/env bash
# Fails if the committed generated code does not match what the protos and
# the pinned generators produce. Run from the prototype root via
# `make check-generated`; needs buf, the pinned protoc-gen-go and
# protoc-gen-go-grpc on PATH, and the client's uv environment.
set -euo pipefail

: "${PROTOC_GEN_GO_VERSION:?set by the Makefile}"
: "${PROTOC_GEN_GO_GRPC_VERSION:?set by the Makefile}"
: "${BUF:=buf}"
: "${UV:=uv}"

fail() { echo "check-generated: $*" >&2; exit 1; }

# Different plugin versions generate different code, so a version mismatch
# would show up as drift with a misleading diff. Say so plainly instead.
got_go="$(protoc-gen-go --version 2>/dev/null | awk '{print $2}')" ||
  fail "protoc-gen-go not on PATH"
[[ "$got_go" == "$PROTOC_GEN_GO_VERSION" ]] ||
  fail "protoc-gen-go is ${got_go:-missing}, want $PROTOC_GEN_GO_VERSION" \
    "(go install google.golang.org/protobuf/cmd/protoc-gen-go@$PROTOC_GEN_GO_VERSION)"
got_grpc="$(protoc-gen-go-grpc --version 2>/dev/null | awk '{print "v" $2}')" ||
  fail "protoc-gen-go-grpc not on PATH"
[[ "$got_grpc" == "$PROTOC_GEN_GO_GRPC_VERSION" ]] ||
  fail "protoc-gen-go-grpc is ${got_grpc:-missing}, want $PROTOC_GEN_GO_GRPC_VERSION" \
    "(go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@$PROTOC_GEN_GO_GRPC_VERSION)"

"$BUF" lint

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

"$BUF" generate -o "$tmp"
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
