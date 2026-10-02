package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
)

// When this variable is set, the test binary behaves as the real command. That lets
// TestInterruptSignalExitsCleanly send a real SIGINT to a real process without a separate build.
const runMainEnv = "GO_CONCURRENCY_RUN_MAIN"

func TestMain(m *testing.M) {
	if os.Getenv(runMainEnv) == "1" {
		main()
		return
	}
	os.Exit(m.Run())
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
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	var stdout, stderr bytes.Buffer
	code := run(ctx, []string{"-jobs", "100", "-work", "1s"}, &stdout, &stderr)
	if code != exitInterrupted {
		t.Errorf("run() = %d, want %d", code, exitInterrupted)
	}
	if !strings.Contains(stderr.String(), "interrupted: finished 0 of 100 jobs") {
		t.Errorf("stderr = %q, want an interruption notice", &stderr)
	}
	if !strings.Contains(stdout.String(), "consumed 0/100 jobs") {
		t.Errorf("stdout = %q, want the partial summary", &stdout)
	}
}

// TestInterruptSignalExitsCleanly checks the real signal wiring end to end: a running process that
// receives SIGINT or SIGTERM stops its goroutines, prints the partial summary and exits with
// status 130, instead of being killed by the default signal action.
func TestInterruptSignalExitsCleanly(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("os.Process.Signal(os.Interrupt) is not supported on Windows")
	}
	for _, sig := range []syscall.Signal{syscall.SIGINT, syscall.SIGTERM} {
		t.Run(sig.String(), func(t *testing.T) {
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
			if err := cmd.Process.Signal(sig); err != nil {
				t.Fatal(err)
			}
			var rest strings.Builder
			for lines.Scan() {
				rest.WriteString(lines.Text() + "\n")
			}
			err = cmd.Wait()
			if !killer.Stop() {
				t.Fatalf("process still running 10s after %v; killed it", sig)
			}

			var exitErr *exec.ExitError
			if !errors.As(err, &exitErr) || exitErr.ExitCode() != exitInterrupted {
				t.Fatalf("exit = %v, want status %d\nstderr:\n%s", err, exitInterrupted, &stderr)
			}
			if !strings.Contains(rest.String(), "consumed 0/1000000 jobs") {
				t.Errorf("stdout after %v = %q, want the partial summary", sig, rest.String())
			}
			if !strings.Contains(stderr.String(), "interrupted:") {
				t.Errorf("stderr = %q, want an interruption notice", &stderr)
			}
		})
	}
}
