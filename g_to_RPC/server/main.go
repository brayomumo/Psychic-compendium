// Command productserver serves ecommerce.v1.ProductCatalogService and the
// standard gRPC health service.
//
// Usage:
//
//	productserver [-addr host:port] [-shutdown-timeout D] [-max-products N]
//	              [-max-recv-bytes N] [-reflection] [-v]
//
// Once it is listening it prints "listening on <addr>" to stdout. SIGINT or
// SIGTERM starts a graceful shutdown: health turns NOT_SERVING, new RPCs are
// refused and in-flight RPCs finish, up to -shutdown-timeout, after which
// the rest are cancelled. Exit status: 1 on a runtime failure, 2 on a usage
// error, and 128+n after a shutdown caused by signal n (130 for SIGINT, 143
// for SIGTERM).
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

	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/catalog"
	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/rpcserver"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

// Flag bounds.
const (
	maxShutdownTimeout = 5 * time.Minute
	maxProductsLimit   = 10_000_000
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run is main without the process-global parts, so tests can call it.
func run(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("productserver", flag.ContinueOnError)
	flags.SetOutput(stderr)
	addr := flags.String("addr", "127.0.0.1:50059", "`host:port` to listen on; port 0 picks a free port")
	shutdownTimeout := flags.Duration("shutdown-timeout", 10*time.Second, "how long in-flight RPCs may run after a shutdown signal")
	maxProducts := flags.Int("max-products", catalog.DefaultMaxProducts, "most products held at once")
	maxRecvBytes := flags.Int("max-recv-bytes", rpcserver.DefaultMaxRecvMsgBytes, "largest request message accepted, in bytes")
	withReflection := flags.Bool("reflection", false, "register the server reflection service (for grpcurl)")
	verbose := flags.Bool("v", false, "log at debug level")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitUsage
	}
	if flags.NArg() > 0 {
		fmt.Fprintf(stderr, "productserver: unexpected argument %q\n", flags.Arg(0))
		flags.Usage()
		return exitUsage
	}
	var problems []error
	if *shutdownTimeout <= 0 || *shutdownTimeout > maxShutdownTimeout {
		problems = append(problems, fmt.Errorf("-shutdown-timeout must be in (0, %v], got %v", maxShutdownTimeout, *shutdownTimeout))
	}
	if *maxProducts < 1 || *maxProducts > maxProductsLimit {
		problems = append(problems, fmt.Errorf("-max-products must be in [1, %d], got %d", maxProductsLimit, *maxProducts))
	}
	if *maxRecvBytes < rpcserver.MinMaxRecvMsgBytes || *maxRecvBytes > rpcserver.MaxMaxRecvMsgBytes {
		problems = append(problems, fmt.Errorf("-max-recv-bytes must be in [%d, %d], got %d",
			rpcserver.MinMaxRecvMsgBytes, rpcserver.MaxMaxRecvMsgBytes, *maxRecvBytes))
	}
	if err := errors.Join(problems...); err != nil {
		fmt.Fprintf(stderr, "productserver: invalid flags:\n%v\n", err)
		flags.Usage()
		return exitUsage
	}

	level := slog.LevelInfo
	if *verbose {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewTextHandler(stderr, &slog.HandlerOptions{Level: level}))

	ctx, stop := notifyContext()
	defer stop()

	srv, err := rpcserver.New(rpcserver.Config{
		Catalog:         catalog.New(*maxProducts, nil),
		Logger:          logger,
		MaxRecvMsgBytes: *maxRecvBytes,
		Reflection:      *withReflection,
	})
	if err != nil {
		logger.Error("build server", "error", err)
		return exitFailure
	}
	lis, err := net.Listen("tcp", *addr)
	if err != nil {
		logger.Error("listen", "addr", *addr, "error", err)
		return exitFailure
	}
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(lis) }()
	// Readiness signal: printed after the signal handler is installed and the
	// socket is listening, so a supervisor or test can rely on both.
	fmt.Fprintf(stdout, "listening on %s\n", lis.Addr())

	select {
	case err := <-serveErr:
		logger.Error("serve", "error", err)
		return exitFailure
	case <-ctx.Done():
	}

	var sig signalError
	errors.As(context.Cause(ctx), &sig)
	logger.Info("shutting down", "signal", sig.sig.String(), "timeout", *shutdownTimeout)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), *shutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Warn("shutdown deadline passed; cancelled the remaining RPCs", "error", err)
	}
	if err := <-serveErr; err != nil {
		logger.Error("serve", "error", err)
		return exitFailure
	}
	logger.Info("stopped")
	return 128 + int(sig.sig)
}

// signalError is the cancellation cause recorded when a signal arrives.
type signalError struct{ sig syscall.Signal }

func (e signalError) Error() string { return "received " + e.sig.String() }

// notifyContext returns a context cancelled by SIGINT or SIGTERM, with the
// signal as the cancellation cause. signal.NotifyContext would not say which
// signal arrived, and the exit status follows the 128+n convention.
//
// After the first signal, handling is reset to the default, so a second
// Ctrl+C terminates immediately if shutdown ever hangs.
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
