module github.com/brayomumo/Psychic-compendium/simple-go-rest-api

// go 1.26.0: the oldest supported Go release, and also the minimum that
// golang.org/x/text v0.42.0 and golang.org/x/sync v0.23.0 declare (pgx
// v5.11.0 itself needs 1.25.0). go mod tidy keeps the .0 form for that reason.
go 1.26.0

toolchain go1.27.1

require github.com/jackc/pgx/v5 v5.11.0

require (
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	github.com/jackc/puddle/v2 v2.2.2 // indirect
	golang.org/x/sync v0.23.0 // indirect
	golang.org/x/text v0.42.0 // indirect
)
