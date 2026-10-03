module github.com/brayomumo/Psychic-compendium/g_to_RPC/server

// go 1.26: the oldest supported Go release, which is the repo's floor.
// grpc-go itself needs only go 1.25.
go 1.26.0

toolchain go1.27.1

require (
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260928230214-8a89bd6388cc
	// v1.83.2, not the newer v1.84.0: v1.84.0 shipped without the fix for
	// GO-2026-6443 (CVE-2026-84445), which govulncheck reports as reachable.
	// The fix is released only as v1.82.2 and v1.83.2 so far. Move to the next
	// 1.84+ release once it includes the fix and `make vulncheck` is clean.
	google.golang.org/grpc v1.83.2
	google.golang.org/protobuf v1.36.12
)

require (
	golang.org/x/net v0.59.0 // indirect
	golang.org/x/sys v0.48.0 // indirect
	golang.org/x/text v0.42.0 // indirect
)
