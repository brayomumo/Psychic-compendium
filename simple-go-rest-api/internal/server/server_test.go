package server

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"testing"
	"time"
)

var discard = slog.New(slog.NewTextHandler(io.Discard, nil))

// hangGuard fails the test instead of letting it block forever.
const hangGuard = 10 * time.Second

func listen(t *testing.T) net.Listener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return ln
}

func startServe(ctx context.Context, t *testing.T, ln net.Listener, h http.Handler, cfg Config) <-chan error {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- Serve(ctx, ln, h, cfg, discard) }()
	return done
}

func waitErr(t *testing.T, done <-chan error) error {
	t.Helper()
	select {
	case err := <-done:
		return err
	case <-time.After(hangGuard):
		t.Fatal("Serve did not return")
		return nil
	}
}

func TestServeShutsDownCleanlyWhenIdle(t *testing.T) {
	ln := listen(t)
	addr := ln.Addr().String()
	ctx, cancel := context.WithCancel(context.Background())
	done := startServe(ctx, t, ln, http.NotFoundHandler(), DefaultConfig())

	resp, err := http.Get("http://" + addr + "/")
	if err != nil {
		t.Fatalf("server not serving: %v", err)
	}
	resp.Body.Close()

	cancel()
	if err := waitErr(t, done); err != nil {
		t.Fatalf("Serve = %v, want nil after a clean shutdown", err)
	}
	if conn, err := net.Dial("tcp", addr); err == nil {
		conn.Close()
		t.Error("listener still accepts connections after shutdown")
	}
}

// The in-flight request must finish successfully, with a live context,
// even though shutdown began while it was running.
func TestShutdownLetsInFlightRequestsFinish(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	ctxErrAtRelease := make(chan error, 1)
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-release
		ctxErrAtRelease <- r.Context().Err()
		w.WriteHeader(http.StatusCreated)
	})
	ln := listen(t)
	addr := ln.Addr().String()
	ctx, cancel := context.WithCancel(context.Background())
	done := startServe(ctx, t, ln, h, DefaultConfig())

	status := make(chan int, 1)
	go func() {
		resp, err := http.Post("http://"+addr+"/slow", "application/json", nil)
		if err != nil {
			status <- -1
			return
		}
		resp.Body.Close()
		status <- resp.StatusCode
	}()
	<-entered
	cancel() // the shutdown signal arrives mid-request

	// Shutdown is draining: new connections are refused, but the request
	// in flight is still running. Wait (bounded) for the listener to close.
	deadline := time.Now().Add(hangGuard)
	for {
		conn, err := net.Dial("tcp", addr)
		if err != nil {
			break
		}
		conn.Close()
		if time.Now().After(deadline) {
			t.Fatal("listener still open during shutdown")
		}
		time.Sleep(5 * time.Millisecond) // observing, not synchronizing
	}
	close(release)

	if got := <-status; got != http.StatusCreated {
		t.Errorf("in-flight request got %d, want 201", got)
	}
	if err := <-ctxErrAtRelease; err != nil {
		t.Errorf("in-flight request's context was cancelled by the shutdown signal: %v", err)
	}
	if err := waitErr(t, done); err != nil {
		t.Errorf("Serve = %v, want nil", err)
	}
}

func TestShutdownTimeoutCutsOffStuckRequests(t *testing.T) {
	entered := make(chan struct{})
	handlerDone := make(chan struct{})
	h := http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		defer close(handlerDone)
		close(entered)
		<-r.Context().Done() // stuck until the server gives up on it
	})
	ln := listen(t)
	addr := ln.Addr().String()
	cfg := DefaultConfig()
	cfg.ShutdownTimeout = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	done := startServe(ctx, t, ln, h, cfg)

	go func() {
		if resp, err := http.Get("http://" + addr + "/stuck"); err == nil {
			resp.Body.Close()
		}
	}()
	<-entered
	cancel()

	err := waitErr(t, done)
	if !errors.Is(err, ErrShutdownTimeout) || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Serve = %v, want ErrShutdownTimeout wrapping DeadlineExceeded", err)
	}
	select {
	case <-handlerDone: // Close cancelled its context, so it did not leak
	case <-time.After(hangGuard):
		t.Error("stuck handler was never cancelled")
	}
}

func TestServeReturnsListenerErrors(t *testing.T) {
	ln := listen(t)
	ln.Close() // Serve cannot accept on a closed listener
	err := waitErr(t, startServe(context.Background(), t, ln, http.NotFoundHandler(), DefaultConfig()))
	if err == nil || errors.Is(err, ErrShutdownTimeout) {
		t.Errorf("Serve on a closed listener = %v, want the accept error", err)
	}
}

func TestDefaultConfigSetsEveryTimeout(t *testing.T) {
	c := DefaultConfig()
	for name, d := range map[string]time.Duration{
		"ReadHeaderTimeout": c.ReadHeaderTimeout, "ReadTimeout": c.ReadTimeout,
		"WriteTimeout": c.WriteTimeout, "IdleTimeout": c.IdleTimeout, "ShutdownTimeout": c.ShutdownTimeout,
	} {
		if d <= 0 {
			t.Errorf("%s = %v; zero means no limit in net/http", name, d)
		}
	}
	if c.MaxHeaderBytes <= 0 {
		t.Error("MaxHeaderBytes must be set")
	}
}
