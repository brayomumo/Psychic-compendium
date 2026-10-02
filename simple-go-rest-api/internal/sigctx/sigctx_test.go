package sigctx

import (
	"context"
	"errors"
	"syscall"
	"testing"
)

func TestSignalReadsTheCause(t *testing.T) {
	ctx, cancel := context.WithCancelCause(context.Background())
	cancel(signalError{sig: syscall.SIGTERM})
	if got := Signal(ctx); got != syscall.SIGTERM {
		t.Errorf("Signal = %v, want SIGTERM", got)
	}
	if got := ExitCode(Signal(ctx)); got != 143 {
		t.Errorf("ExitCode = %d, want 143", got)
	}
}

func TestSignalIsZeroWithoutASignal(t *testing.T) {
	ctx, cancel := context.WithCancelCause(context.Background())
	if Signal(ctx) != 0 {
		t.Error("live context reports a signal")
	}
	cancel(errors.New("something else"))
	if Signal(ctx) != 0 {
		t.Error("non-signal cancellation reported as a signal")
	}
}

func TestStopCancelsWithoutASignal(t *testing.T) {
	ctx, stop := NotifyContext()
	stop()
	<-ctx.Done()
	if Signal(ctx) != 0 {
		t.Errorf("stop() recorded a signal: %v", context.Cause(ctx))
	}
}
