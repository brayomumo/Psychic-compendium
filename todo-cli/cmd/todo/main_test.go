package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// runMainEnv makes the test binary run as a child process for
// signal_test.go: "1" runs the real todo command, "hung-shutdown" runs the
// real signal handling in front of a shutdown that never finishes.
const runMainEnv = "TODO_TEST_RUN_MAIN"

func TestMain(m *testing.M) {
	switch os.Getenv(runMainEnv) {
	case "1":
		main() // exits
	case "hung-shutdown":
		hungShutdown()
	}
	os.Exit(m.Run())
}

// hungShutdown installs the production signal handling, announces readiness,
// and after the first signal never finishes shutting down. Only a second
// signal can end it. The notice goes to stdout so the test can wait for it.
func hungShutdown() {
	ctx, stop := notifyContext(log.New(os.Stdout, "todo: ", 0))
	defer stop()
	fmt.Println("ready")
	<-ctx.Done()
	time.Sleep(time.Hour)
}

type cli struct {
	code        int
	out, errOut string
}

func runCLI(t *testing.T, stdin string, args ...string) cli {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run(context.Background(), args, strings.NewReader(stdin), &out, &errOut)
	return cli{code: code, out: out.String(), errOut: errOut.String()}
}

func TestCancellationCauseSelectsExitStatus(t *testing.T) {
	tests := []struct {
		name     string
		cause    error
		wantCode int
		wantErr  string
	}{
		{"SIGINT", signalError{sig: syscall.SIGINT}, 130, "stopped by interrupt; all confirmed changes are saved"},
		{"SIGTERM", signalError{sig: syscall.SIGTERM}, 143, "stopped by terminated; all confirmed changes are saved"},
		// A cancellation no signal caused is a failure, not a clean shutdown.
		{"internal cancel", errors.New("internal"), exitFailure, "todo: internal"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancelCause(context.Background())
			cancel(tc.cause)
			var out, errOut bytes.Buffer
			path := filepath.Join(t.TempDir(), "tasks.json")
			code := run(ctx, []string{"-file", path}, strings.NewReader("e\nmilk\n\n"), &out, &errOut)
			if code != tc.wantCode {
				t.Errorf("exit code = %d, want %d", code, tc.wantCode)
			}
			if !strings.Contains(errOut.String(), tc.wantErr) {
				t.Errorf("stderr = %q, want it to contain %q", errOut.String(), tc.wantErr)
			}
			if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
				t.Errorf("a cancelled session ran a command (data file stat: %v)", err)
			}
		})
	}
}

func TestUsage(t *testing.T) {
	tests := []struct {
		name     string
		args     []string
		wantCode int
		wantErr  string
	}{
		{"help", []string{"-h"}, exitOK, "Usage: todo [-file path]"},
		{"unknown flag", []string{"-verbose"}, exitUsage, "flag provided but not defined: -verbose"},
		{"flag missing value", []string{"-file"}, exitUsage, "flag needs an argument"},
		{"positional argument", []string{"-file", "x.json", "add"}, exitUsage, `unexpected argument "add"`},
		{"empty file", []string{"-file", ""}, exitUsage, "-file must not be empty"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := runCLI(t, "", tc.args...)
			if r.code != tc.wantCode {
				t.Errorf("exit code = %d, want %d", r.code, tc.wantCode)
			}
			if !strings.Contains(r.errOut, tc.wantErr) {
				t.Errorf("stderr = %q, want it to contain %q", r.errOut, tc.wantErr)
			}
			if r.out != "" {
				t.Errorf("stdout = %q, want nothing on a usage error", r.out)
			}
		})
	}
}

func TestSessionPersistsAcrossRuns(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	if r := runCLI(t, "e\nBuy milk\n2 litres\nq\n", "-file", path); r.code != exitOK {
		t.Fatalf("first run exit code = %d, stderr = %q", r.code, r.errOut)
	}
	r := runCLI(t, "a\n", "-file", path)
	if r.code != exitOK {
		t.Fatalf("second run exit code = %d, stderr = %q", r.code, r.errOut)
	}
	if !strings.Contains(r.out, "Buy milk") || !strings.Contains(r.out, "2 litres") {
		t.Errorf("second run did not see the saved task:\n%s", r.out)
	}
	if !strings.Contains(r.errOut, "tasks are saved to "+path) {
		t.Errorf("stderr = %q, want the data file location", r.errOut)
	}
}

func TestStreamsAreSeparated(t *testing.T) {
	r := runCLI(t, "x\nf\n7\n", "-file", filepath.Join(t.TempDir(), "tasks.json"))
	if strings.Contains(r.out, "unknown command") || strings.Contains(r.out, "error:") {
		t.Errorf("diagnostics leaked to stdout:\n%s", r.out)
	}
	if !strings.Contains(r.errOut, "unknown command") {
		t.Errorf("stderr = %q, want the rejected command reported", r.errOut)
	}
}

func TestCorruptFileIsReportedAndLeftAlone(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	const corrupt = `{"version": 1, "next_id": 2, "tasks": [`
	if err := os.WriteFile(path, []byte(corrupt), 0o600); err != nil {
		t.Fatal(err)
	}
	// Input that would add a task if a session were (wrongly) started.
	r := runCLI(t, "e\nnew\n\n", "-file", path)
	if r.code != exitFailure {
		t.Errorf("exit code = %d, want %d", r.code, exitFailure)
	}
	for _, want := range []string{"corrupt data file " + path, "the file was not modified"} {
		if !strings.Contains(r.errOut, want) {
			t.Errorf("stderr = %q, want it to contain %q", r.errOut, want)
		}
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != corrupt {
		t.Errorf("corrupt file was modified: %q (err %v)", got, err)
	}
}

func TestUnreadableFileIsAFailure(t *testing.T) {
	r := runCLI(t, "", "-file", t.TempDir()) // a directory
	if r.code != exitFailure || !strings.Contains(r.errOut, "read tasks") {
		t.Errorf("exit code = %d, stderr = %q; want %d and a read error", r.code, r.errOut, exitFailure)
	}
}

func TestDefaultFileIsPerUser(t *testing.T) {
	t.Setenv("HOME", "/home/someone")
	t.Setenv("XDG_CONFIG_HOME", "")
	got, err := defaultFile()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got, "/home/someone/") || !strings.HasSuffix(got, filepath.Join("todo-cli", "tasks.json")) {
		t.Errorf("defaultFile() = %q, want a todo-cli/tasks.json under the home directory", got)
	}
}

func TestNoDefaultLocationRequiresFileFlag(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("XDG_CONFIG_HOME", "")
	t.Setenv("AppData", "")
	r := runCLI(t, "")
	if r.code != exitUsage || !strings.Contains(r.errOut, "pass -file") {
		t.Errorf("exit code = %d, stderr = %q; want %d asking for -file", r.code, r.errOut, exitUsage)
	}
}
