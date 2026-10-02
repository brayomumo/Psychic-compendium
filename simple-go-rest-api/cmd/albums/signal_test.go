//go:build unix

package main

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// hangGuard fails a test instead of letting a stuck process block it.
const hangGuard = 15 * time.Second

// lockedBuffer collects a child's stderr while it is still writing.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

type child struct {
	cmd    *exec.Cmd
	url    string
	stderr *lockedBuffer
	exited chan struct{}
}

// startServer runs this test binary as the albums command with an
// in-memory store on a free port, and waits for its readiness banner.
func startServer(t *testing.T) *child {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-store", "memory", "-addr", "127.0.0.1:0")
	cmd.Env = append(os.Environ(),
		"REST_API_TEST_RUN_MAIN=1",
		"GORACE=atexit_sleep_ms=0", // a -race binary otherwise sleeps 1 s before exiting
	)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	c := &child{cmd: cmd, stderr: &lockedBuffer{}, exited: make(chan struct{})}
	cmd.Stderr = c.stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	kill := time.AfterFunc(hangGuard, func() { _ = cmd.Process.Kill() })
	t.Cleanup(func() {
		kill.Stop()
		_ = cmd.Process.Kill()
		<-c.exited
	})

	banner := make(chan string, 1)
	go func() {
		sc := bufio.NewScanner(stdout)
		if sc.Scan() {
			banner <- sc.Text()
		}
		close(banner)
		_, _ = io.Copy(io.Discard, stdout)
	}()
	go func() {
		_ = cmd.Wait()
		close(c.exited)
	}()

	line, ok := <-banner
	url, found := strings.CutPrefix(line, "listening on ")
	if !ok || !found {
		t.Fatalf("no readiness banner; stdout %q, stderr:\n%s", line, c.stderr)
	}
	c.url = url
	return c
}

func (c *child) waitExit(t *testing.T) int {
	t.Helper()
	select {
	case <-c.exited:
	case <-time.After(hangGuard):
		t.Fatalf("server did not exit; stderr:\n%s", c.stderr)
	}
	var exitErr *exec.ExitError
	if err := c.cmd.Err; err != nil && !errors.As(err, &exitErr) {
		t.Fatalf("wait: %v", err)
	}
	return c.cmd.ProcessState.ExitCode()
}

func TestSignalsShutDownCleanlyWith128PlusN(t *testing.T) {
	for _, tt := range []struct {
		sig  syscall.Signal
		want int
	}{
		{syscall.SIGINT, 130},
		{syscall.SIGTERM, 143},
	} {
		t.Run(tt.sig.String(), func(t *testing.T) {
			c := startServer(t)
			resp, err := http.Post(c.url+"/albums", "application/json",
				strings.NewReader(`{"title":"Jeru","artist":"Gerry Mulligan","price_cents":1799}`))
			if err != nil {
				t.Fatalf("POST: %v", err)
			}
			resp.Body.Close()
			if resp.StatusCode != http.StatusCreated {
				t.Fatalf("POST status %d", resp.StatusCode)
			}

			if err := c.cmd.Process.Signal(tt.sig); err != nil {
				t.Fatal(err)
			}
			if got := c.waitExit(t); got != tt.want {
				t.Errorf("exit = %d, want %d; stderr:\n%s", got, tt.want, c.stderr)
			}
			for _, want := range []string{"draining in-flight requests", "shutdown complete", "stopped by signal"} {
				if !strings.Contains(c.stderr.String(), want) {
					t.Errorf("stderr lacks %q:\n%s", want, c.stderr)
				}
			}
		})
	}
}

// A process started from a non-interactive shell with & begins with SIGINT
// ignored. Go's signal.Notify re-enables it, so the server must still stop
// on Ctrl+C. The child inherits the ignored disposition from this process.
func TestSIGINTWorksWhenInheritedAsIgnored(t *testing.T) {
	signal.Ignore(syscall.SIGINT)
	c := startServer(t)
	signal.Reset(syscall.SIGINT)

	if err := c.cmd.Process.Signal(syscall.SIGINT); err != nil {
		t.Fatal(err)
	}
	if got := c.waitExit(t); got != 130 {
		t.Errorf("exit = %d, want 130; stderr:\n%s", got, c.stderr)
	}
}
