//go:build unix

package main

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/store"
)

// child is the todo program running as a real process, so signals are
// delivered and handled exactly as they would be from a terminal.
type child struct {
	cmd        *exec.Cmd
	stdin      io.WriteCloser
	stdoutPipe io.ReadCloser
	stdout     *bufio.Reader
	stderr     *bytes.Buffer
}

func startChild(t *testing.T, path string) *child {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-file", path)
	// Under -race, a child exiting with status 0 sleeps atexit_sleep_ms
	// (1s by default) before exiting; that measures nothing here.
	cmd.Env = append(os.Environ(), runMainEnv+"=1",
		"GORACE="+strings.TrimSpace(os.Getenv("GORACE")+" atexit_sleep_ms=0"))
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	c := &child{cmd: cmd, stdin: stdin, stdoutPipe: stdout, stdout: bufio.NewReader(stdout), stderr: &bytes.Buffer{}}
	cmd.Stderr = c.stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if cmd.ProcessState == nil { // test failed before the child exited
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
		}
	})
	return c
}

// expect reads stdout until it contains want. Reading is how the test knows
// the child is waiting at a prompt; the timeout only guards against a hang.
func (c *child) expect(t *testing.T, want string) {
	t.Helper()
	found := make(chan error, 1)
	go func() {
		var seen []byte
		for !bytes.Contains(seen, []byte(want)) {
			b, err := c.stdout.ReadByte()
			if err != nil {
				found <- err
				return
			}
			seen = append(seen, b)
		}
		found <- nil
	}()
	select {
	case err := <-found:
		if err != nil {
			t.Fatalf("waiting for %q: %v", want, err)
		}
	case <-time.After(10 * time.Second):
		t.Fatalf("timed out waiting for %q", want)
	}
}

func (c *child) send(t *testing.T, input string) {
	t.Helper()
	if _, err := io.WriteString(c.stdin, input); err != nil {
		t.Fatal(err)
	}
}

// waitExit waits for the child and returns its exit code.
func (c *child) waitExit(t *testing.T) int {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- c.cmd.Wait() }()
	select {
	case err := <-done:
		var exitErr *exec.ExitError
		if err != nil && !errors.As(err, &exitErr) {
			t.Fatal(err)
		}
		return c.cmd.ProcessState.ExitCode()
	case <-time.After(10 * time.Second):
		t.Fatal("child did not exit after the signal")
		return -1
	}
}

func TestSignalShutsDownCleanlyAndKeepsData(t *testing.T) {
	tests := []struct {
		name     string
		sig      syscall.Signal
		midAdd   bool // signal arrives while the child waits for a task name
		wantCode int
	}{
		{"SIGINT at the command prompt", syscall.SIGINT, false, 130},
		{"SIGTERM at the command prompt", syscall.SIGTERM, false, 143},
		{"SIGINT halfway through adding a task", syscall.SIGINT, true, 130},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "tasks.json")
			c := startChild(t, path)
			c.expect(t, "> ")
			c.send(t, "e\nBuy milk\n\n")
			c.expect(t, "Added task 1: Buy milk")
			c.expect(t, "> ")
			if tc.midAdd {
				c.send(t, "e\n")
				c.expect(t, "Name: ")
			}

			if err := c.cmd.Process.Signal(tc.sig); err != nil {
				t.Fatal(err)
			}
			if code := c.waitExit(t); code != tc.wantCode {
				t.Errorf("exit code = %d, want %d; stderr:\n%s", code, tc.wantCode, c.stderr)
			}
			if !strings.Contains(c.stderr.String(), "all confirmed changes are saved") {
				t.Errorf("stderr = %q, want a clean shutdown message", c.stderr)
			}

			// The confirmed task survived, the interrupted one was never
			// added, and the file is valid with no temp files left behind.
			l, err := store.NewFile(path).Load()
			if err != nil {
				t.Fatalf("data file unreadable after shutdown: %v", err)
			}
			if all := l.All(); len(all) != 1 || all[0].Name != "Buy milk" {
				t.Errorf("tasks after shutdown = %+v, want only the confirmed task", all)
			}
			if leftovers, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".*.tmp-*")); len(leftovers) > 0 {
				t.Errorf("temporary files left behind: %v", leftovers)
			}
		})
	}
}

func TestEndOfInputExitsZero(t *testing.T) {
	c := startChild(t, filepath.Join(t.TempDir(), "tasks.json"))
	c.expect(t, "> ")
	if err := c.stdin.Close(); err != nil {
		t.Fatal(err)
	}
	if code := c.waitExit(t); code != exitOK {
		t.Errorf("exit code after end of input = %d, want 0; stderr:\n%s", code, c.stderr)
	}
}

// A reader that goes away (todo | head -1) is a local I/O failure: the next
// write fails with EPIPE and the session ends with exit 1 and a message,
// rather than the process being killed by SIGPIPE mid-line. Saved data is
// unaffected either way.
func TestClosedStdoutExitsOneAndKeepsData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	c := startChild(t, path)
	c.send(t, "e\nBuy milk\n\n")
	c.expect(t, "Added task 1: Buy milk")
	if err := c.stdoutPipe.Close(); err != nil {
		t.Fatal(err)
	}
	// Ask for listings until a write fails. Our own writes start failing
	// once the child has exited; that is expected.
	for range 1000 {
		if _, err := io.WriteString(c.stdin, "a\n"); err != nil {
			break
		}
	}
	if code := c.waitExit(t); code != exitFailure {
		t.Errorf("child ended with %v, want exit %d", c.cmd.ProcessState, exitFailure)
	}
	if !strings.Contains(c.stderr.String(), "broken pipe") {
		t.Errorf("stderr = %q, want the write error reported", c.stderr)
	}
	l, err := store.NewFile(path).Load()
	if err != nil || len(l.All()) != 1 {
		t.Errorf("data after closed stdout: %v (err %v), want the saved task", l, err)
	}
}
