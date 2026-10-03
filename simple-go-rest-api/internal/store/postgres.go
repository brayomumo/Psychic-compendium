package store

import (
	"context"
	_ "embed" // for the schema
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/album"
)

//go:embed schema.sql
var schemaSQL string

// migrationLockID is an arbitrary constant. Taking it as a transaction-level
// advisory lock serializes schema changes when several replicas start at
// once; without it, concurrent CREATE TABLE IF NOT EXISTS can still fail
// with a duplicate-key error on the system catalogs.
const migrationLockID int64 = 7_316_402_119

// Postgres error codes this package acts on.
// See https://www.postgresql.org/docs/current/errcodes-appendix.html.
const (
	codeUniqueViolation     = "23505"
	codeInvalidCatalogName  = "3D000" // the database does not exist
	classInvalidAuthorize   = "28"    // bad user or password
	defaultConnectTimeout   = 5 * time.Second
	connectBackoffBase      = 100 * time.Millisecond
	connectBackoffCap       = 2 * time.Second
	defaultHealthCheckEvery = 30 * time.Second
)

// Postgres is an album store backed by PostgreSQL through a connection
// pool. It is safe for concurrent use.
type Postgres struct {
	pool *pgxpool.Pool
}

// OpenPostgres connects to the database at dsn, waits until it answers, and
// applies the schema. Pool size and other pgxpool settings can be tuned in
// the DSN (for example pool_max_conns=10).
//
// The database may still be starting (a container that has just come up),
// so connection failures are retried with capped exponential backoff and
// jitter until ctx is done. Failures that retrying cannot fix, such as a
// wrong password or a missing database, are returned at once.
func OpenPostgres(ctx context.Context, dsn string) (*Postgres, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		// pgx's message can echo the DSN, password included; keep it out of logs.
		return nil, errors.New("store: DATABASE_URL is not a valid PostgreSQL connection string")
	}
	if cfg.ConnConfig.ConnectTimeout == 0 {
		cfg.ConnConfig.ConnectTimeout = defaultConnectTimeout
	}
	// Parameters always travel separately from the SQL (the extended
	// protocol), so the server binds them and they can never change a
	// query's meaning. Only default_query_exec_mode=simple_protocol makes pgx
	// interpolate them into the SQL text client-side. That sanitizer has had
	// two SQL injection advisories (GO-2024-2605, GO-2026-5004), so it is
	// refused even though the pgx in go.mod fixes both: server-side binding
	// rules the whole class out instead of trusting the next fix. The other
	// modes (cache_statement, the default; cache_describe; describe_exec;
	// and exec) all bind server-side and stay available. pgx documents exec
	// as behaving like simple_protocol for applications, and it works
	// behind poolers without prepared-statement support, such as PgBouncer
	// in transaction mode, so the error points there.
	if cfg.ConnConfig.DefaultQueryExecMode == pgx.QueryExecModeSimpleProtocol {
		return nil, errors.New("store: default_query_exec_mode=simple_protocol is not allowed: " +
			"it interpolates parameters into the SQL client-side; use exec, which binds them server-side")
	}
	cfg.HealthCheckPeriod = defaultHealthCheckEvery

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("store: create pool: %w", err)
	}
	if err := waitForDatabase(ctx, pool); err != nil {
		pool.Close()
		return nil, err
	}
	if err := migrate(ctx, pool); err != nil {
		pool.Close()
		return nil, err
	}
	return &Postgres{pool: pool}, nil
}

// waitForDatabase pings until the database answers, a permanent error
// occurs, or ctx is done.
func waitForDatabase(ctx context.Context, pool *pgxpool.Pool) error {
	backoff := connectBackoffBase
	for {
		err := pool.Ping(ctx)
		if err == nil {
			return nil
		}
		if permanent(err) {
			return fmt.Errorf("store: connect: %w", err)
		}
		// Full jitter: replicas restarting together don't retry in lockstep.
		wait := time.Duration(rand.Int64N(int64(backoff)) + 1)
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return fmt.Errorf("store: database did not become reachable: %w (last error: %w)", context.Cause(ctx), err)
		case <-timer.C:
		}
		backoff = min(2*backoff, connectBackoffCap)
	}
}

// permanent reports whether err cannot be fixed by retrying.
func permanent(err error) bool {
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) {
		return false // network-level failures may clear up
	}
	return pgErr.Code == codeInvalidCatalogName || strings.HasPrefix(pgErr.Code, classInvalidAuthorize)
}

func migrate(ctx context.Context, pool *pgxpool.Pool) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("store: migrate: %w", err)
	}
	// Rollback after a successful Commit is a no-op, so this is always safe.
	defer func() { _ = tx.Rollback(context.WithoutCancel(ctx)) }()
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", migrationLockID); err != nil {
		return fmt.Errorf("store: migrate: lock: %w", err)
	}
	if _, err := tx.Exec(ctx, schemaSQL); err != nil {
		return fmt.Errorf("store: migrate: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("store: migrate: commit: %w", err)
	}
	return nil
}

// Close releases every pooled connection. Call it after the HTTP server has
// stopped, so in-flight requests can finish their queries.
func (p *Postgres) Close() { p.pool.Close() }

// Create stores d. It returns album.ErrConflict if an album with the same
// title and artist exists.
func (p *Postgres) Create(ctx context.Context, d album.Draft) (album.Album, error) {
	a := album.Album{Title: d.Title(), Artist: d.Artist(), PriceCents: d.PriceCents()}
	err := p.pool.QueryRow(ctx,
		`INSERT INTO albums (title, artist, price_cents) VALUES ($1, $2, $3) RETURNING id, created_at`,
		a.Title, a.Artist, a.PriceCents,
	).Scan(&a.ID, &a.CreatedAt)
	var pgErr *pgconn.PgError
	switch {
	case errors.As(err, &pgErr) && pgErr.Code == codeUniqueViolation:
		return album.Album{}, album.ErrConflict
	case err != nil:
		return album.Album{}, fmt.Errorf("store: create album: %w", err)
	}
	a.CreatedAt = a.CreatedAt.UTC()
	return a, nil
}

// Get returns the album with the given ID, or album.ErrNotFound.
func (p *Postgres) Get(ctx context.Context, id int64) (album.Album, error) {
	rows, err := p.pool.Query(ctx,
		`SELECT id, title, artist, price_cents, created_at FROM albums WHERE id = $1`, id)
	if err != nil {
		return album.Album{}, fmt.Errorf("store: get album %d: %w", id, err)
	}
	a, err := pgx.CollectExactlyOneRow(rows, pgx.RowToStructByPos[album.Album])
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		return album.Album{}, album.ErrNotFound
	case err != nil:
		return album.Album{}, fmt.Errorf("store: get album %d: %w", id, err)
	}
	a.CreatedAt = a.CreatedAt.UTC()
	return a, nil
}

// List returns up to limit albums with IDs greater than afterID, in ID
// order (keyset pagination: stable under concurrent inserts, and an index
// seek rather than an OFFSET scan). limit must be positive.
func (p *Postgres) List(ctx context.Context, afterID int64, limit int) ([]album.Album, error) {
	if limit < 1 {
		return nil, fmt.Errorf("store: limit must be positive, got %d", limit)
	}
	rows, err := p.pool.Query(ctx,
		`SELECT id, title, artist, price_cents, created_at FROM albums WHERE id > $1 ORDER BY id LIMIT $2`,
		afterID, limit)
	if err != nil {
		return nil, fmt.Errorf("store: list albums: %w", err)
	}
	albums, err := pgx.CollectRows(rows, pgx.RowToStructByPos[album.Album])
	if err != nil {
		return nil, fmt.Errorf("store: list albums: %w", err)
	}
	for i := range albums {
		albums[i].CreatedAt = albums[i].CreatedAt.UTC()
	}
	return albums, nil
}

// Ping reports whether the database answers.
func (p *Postgres) Ping(ctx context.Context) error {
	if err := p.pool.Ping(ctx); err != nil {
		return fmt.Errorf("store: ping: %w", err)
	}
	return nil
}
