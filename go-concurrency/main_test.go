package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
)

// When runMainEnv is set, the test binary behaves as the real command, so tests can send real
// signals to a real process without a separate build. hungShutdownEnv runs hungShutdown instead.
const (
	runMainEnv      = "GO_CONCURRENCY_RUN_MAIN"
	hungShutdownEnv = "GO_CONCURRENCY_HUNG_SHUTDOWN"
)

func TestMain(m *testing.M) {
	switch {
	case os.Getenv(runMainEnv) == "1":
		main()
	case os.Getenv(hungShutdownEnv) == "1":
		hungShutdown()
	default:
		os.Exit(m.Run())
	}
}

// hungShutdown stands in for a shutdown that never finishes: it reports the first signal and then
// never exits on its own. Only a second signal, with default handling restored, can end it.
func hungShutdown() {
	ctx, stop := notifyContext()
	defer stop()
	fmt.Println("ready")
	<-ctx.Done()
	fmt.Println("cancelled")
	time.Sleep(time.Hour) // a sleeping goroutine also keeps the runtime's deadlock detector quiet
}

func TestRunExitCodes(t *testing.T) {
	tests := []struct {
		name       string
		args       []string
		wantCode   int
		wantStdout string // substring
		wantStderr string // substring
	}{
		{"completes", []string{"-jobs", "20", "-work", "0"}, exitOK, "consumed 20/20 jobs", ""},
		{"zero jobs", []string{"-jobs", "0"}, exitOK, "consumed 0/0 jobs", ""},
		{"unbuffered", []string{"-jobs", "20", "-buffer", "0", "-work", "0"}, exitOK, "consumed 20/20 jobs", ""},
		// Regression: the first version panicked in rand.Intn(0) with more consumers than jobs.
		{"more consumers than jobs", []string{"-consumers", "150", "-jobs", "100", "-work", "0"}, exitOK, "consumed 100/100 jobs", ""},
		{"help", []string{"-h"}, exitOK, "", "Usage of go-concurrency"},
		// Regression: the first version panicked with "sync: negative WaitGroup counter".
		{"negative consumers", []string{"-consumers", "-2"}, exitUsage, "", "consumers must be in [1, 10000], got -2"},
		{"zero producers", []string{"-producers", "0"}, exitUsage, "", "producers must be in"},
		{"negative buffer", []string{"-buffer", "-1"}, exitUsage, "", "buffer must be in"},
		{"several bad flags reported together", []string{"-jobs", "-1", "-work", "-1s"}, exitUsage, "", "work must be in"},
		{"malformed number", []string{"-jobs", "lots"}, exitUsage, "", `invalid value "lots"`},
		{"malformed duration", []string{"-work", "fast"}, exitUsage, "", `invalid value "fast"`},
		{"unknown flag", []string{"-consumer", "3"}, exitUsage, "", "flag provided but not defined: -consumer"},
		{"positional argument", []string{"extra"}, exitUsage, "", `unexpected arguments: ["extra"]`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := run(context.Background(), tt.args, &stdout, &stderr)
			if code != tt.wantCode {
				t.Errorf("run(%q) = %d, want %d\nstdout:\n%s\nstderr:\n%s", tt.args, code, tt.wantCode, &stdout, &stderr)
			}
			if !strings.Contains(stdout.String(), tt.wantStdout) {
				t.Errorf("stdout = %q, want it to contain %q", &stdout, tt.wantStdout)
			}
			if !strings.Contains(stderr.String(), tt.wantStderr) {
				t.Errorf("stderr = %q, want it to contain %q", &stderr, tt.wantStderr)
			}
		})
	}
}

func TestRunVerboseLogsEveryJob(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(context.Background(), []string{"-jobs", "7", "-work", "0", "-v"}, &stdout, &stderr); code != exitOK {
		t.Fatalf("run() = %d, want %d; stderr:\n%s", code, exitOK, &stderr)
	}
	if got := strings.Count(stderr.String(), "finished job"); got != 7 {
		t.Errorf("verbose log has %d job lines, want 7:\n%s", got, &stderr)
	}
}

func TestRunReportsInterruption(t *testing.T) {
	tests := []struct {
		name       string
		cause      error
		wantCode   int
		wantStderr string
	}{
		{"SIGINT", signalError{sig: syscall.SIGINT}, 130, "received interrupt: finished 0 of 100 jobs"},
		{"SIGTERM", signalError{sig: syscall.SIGTERM}, 143, "received terminated: finished 0 of 100 jobs"},
		// Not reachable from main, which cancels only on a signal, but a cancellation with no
		// signal behind it must not be mistaken for a clean signal shutdown.
		{"no signal", nil, exitFailure, "context canceled: finished 0 of 100 jobs"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			ctx, cancel := context.WithCancelCause(context.Background())
			cancel(tt.cause)
			var stdout, stderr bytes.Buffer
			code := run(ctx, []string{"-jobs", "100", "-work", "1s"}, &stdout, &stderr)
			if code != tt.wantCode {
				t.Errorf("run() = %d, want %d", code, tt.wantCode)
			}
			if !strings.Contains(stderr.String(), tt.wantStderr) {
				t.Errorf("stderr = %q, want it to contain %q", &stderr, tt.wantStderr)
			}
			if !strings.Contains(stdout.String(), "consumed 0/100 jobs") {
				t.Errorf("stdout = %q, want the partial summary", &stdout)
			}
		})
	}
}

func TestNotifyContextRecordsSignalAsCause(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("signals cannot be sent to the current process on Windows")
	}
	for _, sig := range []syscall.Signal{syscall.SIGINT, syscall.SIGTERM} {
		t.Run(sig.String(), func(t *testing.T) {
			ctx, stop := notifyContext()
			defer stop()
			// notifyContext has registered its handler before returning, so this signal is
			// delivered to it rather than killing the test binary.
			if err := syscall.Kill(os.Getpid(), sig); err != nil {
				t.Fatal(err)
			}
			select {
			case <-ctx.Done():
			case <-time.After(5 * time.Second):
				t.Fatalf("context not cancelled 5s after %v", sig)
			}
			var got signalError
			if cause := context.Cause(ctx); !errors.As(cause, &got) || got.sig != sig {
				t.Errorf("context.Cause = %v, want %v", cause, signalError{sig: sig})
			}
		})
	}
}

func TestNotifyContextStopCancelsWithoutSignal(t *testing.T) {
	ctx, stop := notifyContext()
	stop()
	select {
	case <-ctx.Done():
	default:
		t.Fatal("context still live after stop()")
	}
	if cause := context.Cause(ctx); !errors.Is(cause, context.Canceled) {
		t.Errorf("context.Cause = %v, want context.Canceled", cause)
	}
}

// TestInterruptSignalExitsCleanly checks the real signal wiring end to end: a running process that
// receives SIGINT or SIGTERM stops its goroutines, prints the partial summary and exits with
// 128+n (130 or 143), instead of being killed by the default signal action.
func TestInterruptSignalExitsCleanly(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("os.Process.Signal(os.Interrupt) is not supported on Windows")
	}
	tests := []struct {
		sig        syscall.Signal
		wantCode   int
		wantStderr string
	}{
		{syscall.SIGINT, 130, "received interrupt:"},
		{syscall.SIGTERM, 143, "received terminated:"},
	}
	for _, tt := range tests {
		t.Run(tt.sig.String(), func(t *testing.T) {
			// One-hour jobs: none can finish, however late the signal lands.
			cmd := exec.Command(os.Args[0], "-jobs", "1000000", "-work", "1h", "-consumers", "4")
			cmd.Env = append(os.Environ(), runMainEnv+"=1")
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			stdout, err := cmd.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			// If the process hangs, kill it so the test fails instead of blocking forever.
			killer := time.AfterFunc(10*time.Second, func() { _ = cmd.Process.Kill() })

			// main installs the signal handler before run prints its first line, so once that
			// line arrives the signal is guaranteed to reach the handler rather than kill the
			// process.
			lines := bufio.NewScanner(stdout)
			if !lines.Scan() || !strings.HasPrefix(lines.Text(), "running:") {
				_ = cmd.Process.Kill()
				t.Fatalf("first stdout line = %q, want the running: banner", lines.Text())
			}
			if err := cmd.Process.Signal(tt.sig); err != nil {
				t.Fatal(err)
			}
			var rest strings.Builder
			for lines.Scan() {
				rest.WriteString(lines.Text() + "\n")
			}
			err = cmd.Wait()
			if !killer.Stop() {
				t.Fatalf("process still running 10s after %v; killed it", tt.sig)
			}

			var exitErr *exec.ExitError
			if !errors.As(err, &exitErr) || exitErr.ExitCode() != tt.wantCode {
				t.Fatalf("exit = %v, want status %d\nstderr:\n%s", err, tt.wantCode, &stderr)
			}
			if !strings.Contains(rest.String(), "consumed 0/1000000 jobs") {
				t.Errorf("stdout after %v = %q, want the partial summary", tt.sig, rest.String())
			}
			if !strings.Contains(stderr.String(), tt.wantStderr) {
				t.Errorf("stderr = %q, want it to contain %q", &stderr, tt.wantStderr)
			}
		})
	}
}

// The first signal starts a clean shutdown; if that shutdown hangs, a second Ctrl+C must still kill
// the process. notifyContext restores default handling after the first signal, which this checks
// against a process whose shutdown never finishes.
func TestSecondSignalKillsHungShutdown(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("os.Process.Signal(os.Interrupt) is not supported on Windows")
	}
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), hungShutdownEnv+"=1")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	killer := time.AfterFunc(10*time.Second, func() { _ = cmd.Process.Kill() })

	lines := bufio.NewScanner(stdout)
	expect := func(want string) {
		t.Helper()
		if !lines.Scan() || lines.Text() != want {
			_ = cmd.Process.Kill()
			t.Fatalf("stdout line = %q, want %q", lines.Text(), want)
		}
	}
	expect("ready")
	if err := cmd.Process.Signal(syscall.SIGINT); err != nil {
		t.Fatal(err)
	}
	expect("cancelled") // printed only after signal.Stop has run
	if err := cmd.Process.Signal(syscall.SIGINT); err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, stdout) // drain until the process exits, as Wait requires
	err = cmd.Wait()
	if !killer.Stop() {
		t.Fatal("process survived a second SIGINT for 10s; killed it")
	}

	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("Wait() = %v, want the process to die by SIGINT", err)
	}
	ws, ok := exitErr.Sys().(syscall.WaitStatus)
	if !ok || !ws.Signaled() || ws.Signal() != syscall.SIGINT {
		t.Errorf("process ended with %v, want death by SIGINT", exitErr)
	}
}
