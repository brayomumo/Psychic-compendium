module github.com/brayomumo/Psychic-compendium/g_to_RPC/server

// go 1.23 because grpc-go v1.75.x requires it, and v1.75.1 is the newest
// grpc-go that supports the repo's Go 1.23 toolchain (v1.76+ need Go 1.24).
go 1.23.0

require (
	google.golang.org/genproto/googleapis/rpc v0.0.0-20250707201910-8d1bb00bc6a7
	google.golang.org/grpc v1.75.1
	google.golang.org/protobuf v1.36.11
)

require (
	golang.org/x/net v0.41.0 // indirect
	golang.org/x/sys v0.33.0 // indirect
	golang.org/x/text v0.26.0 // indirect
)
