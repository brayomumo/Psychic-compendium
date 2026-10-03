// Package server runs an http.Handler with production timeouts and shuts it
// down gracefully: stop accepting connections, let in-flight requests
// finish, and only cut them off if a deadline passes.
package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"
)

// Config holds the server's timeouts. Each one closes a way for a slow or
// malicious client to hold a connection (and its goroutine and memory)
// forever; http.Server's zero values mean "no limit".
type Config struct {
	// ReadHeaderTimeout bounds reading the request line and headers
	// (Slowloris: a client trickling headers one byte at a time).
	ReadHeaderTimeout time.Duration
	// ReadTimeout bounds reading the whole request, body included.
	ReadTimeout time.Duration
	// WriteTimeout bounds the time from the end of the request headers to
	// the end of the response.
	WriteTimeout time.Duration
	// IdleTimeout bounds how long a keep-alive connection waits for its
	// next request.
	IdleTimeout time.Duration
	// MaxHeaderBytes caps the size of request headers.
	MaxHeaderBytes int
	// ShutdownTimeout bounds the graceful drain after ctx is cancelled.
	ShutdownTimeout time.Duration
}

// DefaultConfig returns timeouts suited to a small JSON API.
func DefaultConfig() Config {
	return Config{
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    16 << 10,
		ShutdownTimeout:   10 * time.Second,
	}
}

// ErrShutdownTimeout reports that requests were still running when the
// graceful-shutdown deadline passed, and were cut off.
var ErrShutdownTimeout = errors.New("graceful shutdown timed out; remaining requests were cut off")

// Serve serves h on ln until ctx is done, then shuts down gracefully. It
// returns nil after a clean shutdown, an error wrapping ErrShutdownTimeout
// if in-flight requests outlived cfg.ShutdownTimeout, or the error that
// stopped the server early. ln is closed when Serve returns.
func Serve(ctx context.Context, ln net.Listener, h http.Handler, cfg Config, log *slog.Logger) error {
	srv := &http.Server{
		Handler:           h,
		ReadHeaderTimeout: cfg.ReadHeaderTimeout,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      cfg.WriteTimeout,
		IdleTimeout:       cfg.IdleTimeout,
		MaxHeaderBytes:    cfg.MaxHeaderBytes,
		ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
		// BaseContext is deliberately left at its default. Deriving request
		// contexts from ctx would cancel every in-flight request the moment
		// a shutdown signal arrives, which is the opposite of a graceful
		// drain. Requests are cancelled only if the drain times out.
	}

	served := make(chan error, 1)
	go func() { served <- srv.Serve(ln) }()

	select {
	case err := <-served:
		return fmt.Errorf("server: %w", err) // never http.ErrServerClosed: only Shutdown causes that
	case <-ctx.Done():
	}

	log.Info("shutting down: no new connections; draining in-flight requests",
		"timeout", cfg.ShutdownTimeout)
	drainCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), cfg.ShutdownTimeout)
	defer cancel()
	err := srv.Shutdown(drainCtx)
	<-served // Serve returns http.ErrServerClosed as soon as Shutdown starts
	if err != nil {
		// Close cancels the remaining requests' contexts and closes their
		// connections, so their goroutines finish rather than leak.
		_ = srv.Close()
		return fmt.Errorf("server: %w after %v: %w", ErrShutdownTimeout, cfg.ShutdownTimeout, err)
	}
	log.Info("shutdown complete")
	return nil
}
