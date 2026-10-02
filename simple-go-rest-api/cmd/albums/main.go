// Command albums serves the albums REST API.
//
// Usage:
//
//	albums [-addr host:port] [-store postgres|memory] [flags]
//
// The PostgreSQL connection string comes from the DATABASE_URL environment
// variable, never from a flag, so the password does not show up in process
// listings. REST_API_ADDR sets the default for -addr.
//
// Exit status: 1 on a startup or runtime failure (including a graceful
// shutdown that ran out of time), 2 on a usage error, and 128+n after a
// clean shutdown on signal n (130 for SIGINT, 143 for SIGTERM).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/api"
	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/server"
	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/store"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

// Bounds for flag values. They reject typos (-request-timeout 2h) rather
// than encode policy.
const (
	maxTimeout      = 10 * time.Minute
	maxBodyBytesCap = 1 << 20
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, os.Getenv))
}

type options struct {
	addr            string
	store           string
	requestTimeout  time.Duration
	shutdownTimeout time.Duration
	startupTimeout  time.Duration
	maxBodyBytes    int64
	logJSON         bool
	databaseURL     string
}

// run is main without the process globals, so tests can call it.
func run(args []string, stdout, stderr io.Writer, getenv func(string) string) int {
	opts, code, ok := parseFlags(args, stderr, getenv)
	if !ok {
		return code
	}
	log := newLogger(stderr, opts.logJSON)
	ctx, stop := notifyContext()
	defer stop()

	st, closeStore, err := openStore(ctx, opts, log)
	if err != nil {
		if sig := signalCause(ctx); sig != 0 {
			log.Warn("interrupted during startup", "signal", sig.String())
			return 128 + int(sig)
		}
		log.Error("startup failed", "error", err)
		return exitFailure
	}
	// Deferred, so the pool closes only after Serve has drained every
	// in-flight request.
	defer closeStore()

	ln, err := net.Listen("tcp", opts.addr)
	if err != nil {
		log.Error("listen failed", "addr", opts.addr, "error", err)
		return exitFailure
	}
	// The banner goes to stdout once the socket is bound and signal handling
	// is installed, so scripts and tests can wait for it (and learn the port
	// when -addr ends in :0).
	fmt.Fprintf(stdout, "listening on http://%s\n", ln.Addr())

	handler := api.New(api.Config{
		Store:          st,
		Logger:         log,
		RequestTimeout: opts.requestTimeout,
		MaxBodyBytes:   opts.maxBodyBytes,
	})
	cfg := server.DefaultConfig()
	cfg.ShutdownTimeout = opts.shutdownTimeout
	if err := server.Serve(ctx, ln, handler, cfg, log); err != nil {
		log.Error("server stopped", "error", err)
		return exitFailure
	}
	if sig := signalCause(ctx); sig != 0 {
		log.Info("stopped by signal", "signal", sig.String())
		return 128 + int(sig)
	}
	return exitOK // Serve returns nil only after ctx is done, so in practice unreachable
}

func parseFlags(args []string, stderr io.Writer, getenv func(string) string) (options, int, bool) {
	var o options
	fs := flag.NewFlagSet("albums", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), `Usage: albums [flags]

Serves the albums REST API. Environment:
  DATABASE_URL   PostgreSQL connection string (required with -store postgres)
  REST_API_ADDR  default for -addr

Flags:
`)
		fs.PrintDefaults()
	}
	defaultAddr := getenv("REST_API_ADDR")
	if defaultAddr == "" {
		// All interfaces: inside a container, "localhost" is the container
		// itself, so binding it makes the API unreachable from outside.
		defaultAddr = ":8080"
	}
	fs.StringVar(&o.addr, "addr", defaultAddr, "`host:port` to listen on; :0 picks a free port")
	fs.StringVar(&o.store, "store", "postgres", "where albums live: postgres or memory")
	fs.DurationVar(&o.requestTimeout, "request-timeout", api.DefaultRequestTimeout, "deadline for each database call")
	fs.DurationVar(&o.shutdownTimeout, "shutdown-timeout", server.DefaultConfig().ShutdownTimeout,
		"how long in-flight requests may run after a shutdown signal")
	fs.DurationVar(&o.startupTimeout, "startup-timeout", 30*time.Second, "how long to wait for the database at startup")
	fs.Int64Var(&o.maxBodyBytes, "max-body-bytes", api.DefaultMaxBodyBytes, "largest accepted request body")
	fs.BoolVar(&o.logJSON, "log-json", false, "write logs as JSON instead of text")

	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return o, exitOK, false
		}
		return o, exitUsage, false // flag has printed the error and usage
	}
	o.databaseURL = getenv("DATABASE_URL")

	var errs []error
	if fs.NArg() > 0 {
		errs = append(errs, fmt.Errorf("unexpected arguments: %q", fs.Args()))
	}
	if _, _, err := net.SplitHostPort(o.addr); err != nil {
		errs = append(errs, fmt.Errorf("-addr %q is not host:port", o.addr))
	}
	switch o.store {
	case "memory":
	case "postgres":
		if o.databaseURL == "" {
			errs = append(errs, errors.New("DATABASE_URL must be set when -store is postgres"))
		}
	default:
		errs = append(errs, fmt.Errorf("-store must be postgres or memory, got %q", o.store))
	}
	for _, t := range []struct {
		name string
		d    time.Duration
	}{
		{"-request-timeout", o.requestTimeout},
		{"-shutdown-timeout", o.shutdownTimeout},
		{"-startup-timeout", o.startupTimeout},
	} {
		if t.d <= 0 || t.d > maxTimeout {
			errs = append(errs, fmt.Errorf("%s must be between 0 and %v, got %v", t.name, maxTimeout, t.d))
		}
	}
	if o.maxBodyBytes < 1 || o.maxBodyBytes > maxBodyBytesCap {
		errs = append(errs, fmt.Errorf("-max-body-bytes must be between 1 and %d, got %d", maxBodyBytesCap, o.maxBodyBytes))
	}
	if err := errors.Join(errs...); err != nil {
		fmt.Fprintf(stderr, "albums: invalid configuration:\n%v\n\n", err)
		fs.Usage()
		return o, exitUsage, false
	}
	return o, exitOK, true
}

func newLogger(w io.Writer, json bool) *slog.Logger {
	if json {
		return slog.New(slog.NewJSONHandler(w, nil))
	}
	return slog.New(slog.NewTextHandler(w, nil))
}

// openStore returns the configured store and a function that releases it.
func openStore(ctx context.Context, o options, log *slog.Logger) (api.Store, func(), error) {
	if o.store == "memory" {
		log.Warn("using the in-memory store: albums are lost when the process exits")
		return store.NewMemory(nil), func() {}, nil
	}
	startCtx, cancel := context.WithTimeout(ctx, o.startupTimeout)
	defer cancel()
	pg, err := store.OpenPostgres(startCtx, o.databaseURL)
	if err != nil {
		return nil, nil, err
	}
	log.Info("connected to PostgreSQL; schema is up to date")
	return pg, pg.Close, nil
}

// signalError is the cancellation cause recorded when a signal arrives.
type signalError struct{ sig syscall.Signal }

func (e signalError) Error() string { return "received " + e.sig.String() }

// signalCause returns the signal that cancelled ctx, or 0.
func signalCause(ctx context.Context) syscall.Signal {
	var se signalError
	if errors.As(context.Cause(ctx), &se) {
		return se.sig
	}
	return 0
}

// notifyContext returns a context cancelled by SIGINT or SIGTERM, with the
// signal as the cancellation cause. signal.NotifyContext cannot say which
// signal arrived, and the exit status follows the 128+n convention.
//
// After the first signal, handling is reset to the default, so a second
// Ctrl+C terminates at once if a graceful shutdown ever hangs.
func notifyContext() (context.Context, func()) {
	ctx, cancel := context.WithCancelCause(context.Background())
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		select {
		case s := <-signals:
			signal.Stop(signals)
			sig, _ := s.(syscall.Signal) // always a syscall.Signal on Unix
			cancel(signalError{sig: sig})
		case <-ctx.Done():
		}
	}()
	return ctx, func() {
		signal.Stop(signals)
		cancel(nil)
	}
}
