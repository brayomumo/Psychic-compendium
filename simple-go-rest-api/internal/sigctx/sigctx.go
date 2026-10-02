// Package sigctx turns SIGINT and SIGTERM into context cancellation that
// remembers which signal arrived, so a command can exit with the
// conventional 128+n status (130 for SIGINT, 143 for SIGTERM).
// signal.NotifyContext cannot say which signal it was.
package sigctx

import (
	"context"
	"errors"
	"os"
	"os/signal"
	"syscall"
)

// signalError is the cancellation cause recorded when a signal arrives.
type signalError struct{ sig syscall.Signal }

func (e signalError) Error() string { return "received " + e.sig.String() }

// NotifyContext returns a context cancelled by SIGINT or SIGTERM, with the
// signal as the cancellation cause, and a function that stops listening.
//
// After the first signal, handling is reset to the default, so a second
// Ctrl+C terminates at once if a graceful shutdown ever hangs.
func NotifyContext() (context.Context, func()) {
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

// Signal returns the signal that cancelled ctx, or 0 if no signal did.
func Signal(ctx context.Context) syscall.Signal {
	var se signalError
	if errors.As(context.Cause(ctx), &se) {
		return se.sig
	}
	return 0
}

// ExitCode returns the conventional exit status for a shutdown caused by
// sig: 128 plus the signal number.
func ExitCode(sig syscall.Signal) int { return 128 + int(sig) }
