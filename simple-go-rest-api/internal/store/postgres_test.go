package store

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// defaultIntegrationDSN matches the compose file's local-only Postgres.
const defaultIntegrationDSN = "postgres://albums:albums@127.0.0.1:5439/albums?sslmode=disable"

// integrationDSN returns the DSN of a reachable test database, or skips the
// test. Integration tests run only when REST_API_INTEGRATION=1, so `make
// check` never needs Docker.
func integrationDSN(t *testing.T) string {
	t.Helper()
	if os.Getenv("REST_API_INTEGRATION") != "1" {
		t.Skip("set REST_API_INTEGRATION=1 (make test-integration) to run against PostgreSQL")
	}
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		dsn = defaultIntegrationDSN
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Skipf("PostgreSQL not reachable (%v); run make db-up", err)
	}
	_ = conn.Close(ctx)
	return dsn
}

// isolatedDSN creates a schema unique to this test and returns a DSN whose
// search_path points at it, so tests never see each other's rows. The
// schema is dropped when the test ends.
func isolatedDSN(t *testing.T) string {
	t.Helper()
	dsn := integrationDSN(t)
	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	schema := "test_" + hex.EncodeToString(buf)

	ctx := context.Background()
	admin, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	t.Cleanup(func() {
		if _, err := admin.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE"); err != nil {
			t.Errorf("drop schema %s: %v", schema, err)
		}
		_ = admin.Close(ctx)
	})

	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatalf("parse DSN: %v", err)
	}
	q := u.Query()
	q.Set("search_path", schema) // pgx passes unknown parameters to the server
	u.RawQuery = q.Encode()
	return u.String()
}

func openIsolated(t *testing.T) *Postgres {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	s, err := OpenPostgres(ctx, isolatedDSN(t))
	if err != nil {
		t.Fatalf("OpenPostgres: %v", err)
	}
	t.Cleanup(s.Close)
	return s
}

func TestPostgresContract(t *testing.T) {
	runContract(t, func(t *testing.T) albumStore { return openIsolated(t) })
}

func TestPostgresMigrationIsIdempotentUnderConcurrentStartup(t *testing.T) {
	dsn := isolatedDSN(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	const replicas = 5
	var wg sync.WaitGroup
	errs := make(chan error, replicas)
	for range replicas {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s, err := OpenPostgres(ctx, dsn)
			if err != nil {
				errs <- err
				return
			}
			s.Close()
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Errorf("concurrent OpenPostgres: %v", err)
	}
}

func TestPostgresSchemaRejectsInvalidRowsWrittenDirectly(t *testing.T) {
	s := openIsolated(t)
	ctx := context.Background()
	for _, stmt := range []string{
		`INSERT INTO albums (title, artist, price_cents) VALUES ('', 'a', 1)`,
		`INSERT INTO albums (title, artist, price_cents) VALUES ('t', 'a', -1)`,
	} {
		if _, err := s.pool.Exec(ctx, stmt); err == nil {
			t.Errorf("%s: accepted, want a CHECK violation", stmt)
		}
	}
}

// The DSN must not be able to switch the pool to client-side parameter
// interpolation, the code path of GO-2026-5004 in pgx 5.7.6.
func TestPostgresNeverUsesTheSimpleProtocol(t *testing.T) {
	dsn := isolatedDSN(t)
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	q := u.Query()
	q.Set("default_query_exec_mode", "simple_protocol")
	u.RawQuery = q.Encode()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	s, err := OpenPostgres(ctx, u.String())
	if err != nil {
		t.Fatalf("OpenPostgres: %v", err)
	}
	defer s.Close()
	if mode := s.pool.Config().ConnConfig.DefaultQueryExecMode; mode != pgx.QueryExecModeCacheStatement {
		t.Errorf("exec mode = %v, want QueryExecModeCacheStatement", mode)
	}
	// A value full of SQL metacharacters must round-trip as data.
	evil := `$$; DROP TABLE albums; --$$ ' "`
	a, err := s.Create(ctx, draft(t, evil, "Artist", 1))
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	got, err := s.Get(ctx, a.ID)
	if err != nil || got.Title != evil {
		t.Errorf("round-trip = %q, %v; want the title unchanged", got.Title, err)
	}
}

func TestOpenPostgresWrongPasswordFailsFast(t *testing.T) {
	dsn := integrationDSN(t)
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	u.User = url.UserPassword(u.User.Username(), "definitely-wrong")
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	start := time.Now()
	_, err = OpenPostgres(ctx, u.String())
	if err == nil {
		t.Fatal("OpenPostgres with a wrong password succeeded")
	}
	if errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 10*time.Second {
		t.Errorf("wrong password was retried until the deadline (%v): %v", time.Since(start), err)
	}
}

// No database needed: nothing listens on port 1, so every attempt is
// refused, and the retry loop must give up when the context does.
func TestOpenPostgresRetriesUntilTheContextEnds(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	_, err := OpenPostgres(ctx, "postgres://u:p@127.0.0.1:1/db?sslmode=disable&connect_timeout=1")
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error = %v, want it to wrap context.DeadlineExceeded", err)
	}
	if !strings.Contains(err.Error(), "did not become reachable") {
		t.Errorf("error %q should say the database never became reachable", err)
	}
}

func TestOpenPostgresInvalidDSNDoesNotLeakThePassword(t *testing.T) {
	_, err := OpenPostgres(context.Background(), "postgres://user:s3cret@host:notaport/db")
	if err == nil {
		t.Fatal("want an error for a malformed DSN")
	}
	if strings.Contains(err.Error(), "s3cret") {
		t.Errorf("error leaks the password: %v", err)
	}
}
