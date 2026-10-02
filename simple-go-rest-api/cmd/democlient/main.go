// Command democlient exercises the albums API end to end and checks every
// response. `make run` and `make docker-smoke` use it.
//
// Usage:
//
//	democlient -server bin/albums [-store postgres] [-addr 127.0.0.1:8089]
//	democlient -base-url http://127.0.0.1:8089
//
// With -server it starts that albums binary itself, waits for its readiness
// banner, runs the scenario, then stops the server with SIGTERM and checks
// that it shut down cleanly (exit 143). Without -server it runs the scenario
// against an API that is already running at -base-url.
//
// Exit status: 0 when every check passes, 1 when any fails, 2 on a usage
// error, and 128+n if interrupted by signal n.
package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/sigctx"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("democlient", flag.ContinueOnError)
	fs.SetOutput(stderr)
	serverBin := fs.String("server", "", "albums `binary` to start and stop; empty means use -base-url")
	storeKind := fs.String("store", "postgres", "store for the started server: postgres or memory")
	addr := fs.String("addr", "127.0.0.1:0", "address for the started server")
	baseURL := fs.String("base-url", "http://127.0.0.1:8089", "API to test when -server is empty")
	timeout := fs.Duration("timeout", 60*time.Second, "overall deadline")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitUsage
	}
	if fs.NArg() > 0 || *timeout <= 0 {
		fmt.Fprintln(stderr, "democlient: unexpected arguments or non-positive -timeout")
		fs.Usage()
		return exitUsage
	}

	sigCtx, stop := sigctx.NotifyContext()
	defer stop()
	ctx, cancel := context.WithTimeout(sigCtx, *timeout)
	defer cancel()

	r := &runner{ctx: ctx, client: &http.Client{Timeout: 10 * time.Second}, out: stdout}
	var srv *server
	if *serverBin != "" {
		var err error
		srv, err = startServer(ctx, *serverBin, *storeKind, *addr, stderr)
		if err != nil {
			fmt.Fprintf(stderr, "democlient: %v\n", err)
			return exitFailure
		}
		defer srv.kill()
		r.base = srv.url
	} else {
		r.base = strings.TrimSuffix(*baseURL, "/")
	}

	err := r.scenario()
	if srv != nil {
		r.checkShutdown(srv)
	}
	if sig := sigctx.Signal(sigCtx); sig != 0 {
		fmt.Fprintf(stderr, "democlient: interrupted by %v\n", sig)
		return sigctx.ExitCode(sig)
	}
	if err != nil {
		fmt.Fprintf(stderr, "democlient: %v\n", err)
		return exitFailure
	}
	fmt.Fprintf(stdout, "\n%d checks, %d failed\n", r.checks, r.failed)
	if r.failed > 0 {
		return exitFailure
	}
	return exitOK
}

// server is an albums process the client started.
type server struct {
	cmd    *exec.Cmd
	url    string
	exited chan struct{}
}

func startServer(ctx context.Context, bin, storeKind, addr string, stderr io.Writer) (*server, error) {
	cmd := exec.Command(bin, "-store", storeKind, "-addr", addr)
	cmd.Stderr = stderr // the server's logs, interleaved with ours on a terminal
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start %s: %w", bin, err)
	}
	s := &server{cmd: cmd, exited: make(chan struct{})}
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
		close(s.exited)
	}()
	select {
	case line, ok := <-banner:
		url, found := strings.CutPrefix(line, "listening on ")
		if !ok || !found {
			s.kill()
			return nil, fmt.Errorf("%s exited before it was ready (see its log above)", bin)
		}
		s.url = url
		return s, nil
	case <-ctx.Done():
		s.kill()
		return nil, fmt.Errorf("waiting for %s to start: %w", bin, context.Cause(ctx))
	}
}

// kill makes sure the server is gone; a no-op once it has exited.
func (s *server) kill() {
	select {
	case <-s.exited:
	default:
		_ = s.cmd.Process.Kill()
		<-s.exited
	}
}

type runner struct {
	ctx    context.Context
	client *http.Client
	base   string
	out    io.Writer
	checks int
	failed int
}

type response struct {
	status int
	header http.Header
	body   string
}

func (r *runner) send(method, path, contentType, body string) (response, error) {
	req, err := http.NewRequestWithContext(r.ctx, method, r.base+path, strings.NewReader(body))
	if err != nil {
		return response{}, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return response{}, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return response{}, err
	}
	return response{status: resp.StatusCode, header: resp.Header, body: string(b)}, nil
}

// expect sends a request and records whether the status (and, if given,
// the body) matched. It returns the response for later steps.
func (r *runner) expect(what, method, path, contentType, body string, wantStatus int, wantInBody string) (response, error) {
	resp, err := r.send(method, path, contentType, body)
	if err != nil {
		return response{}, fmt.Errorf("%s: %w", what, err)
	}
	ok := resp.status == wantStatus && strings.Contains(resp.body, wantInBody)
	r.record(ok, fmt.Sprintf("%-38s %-7s %-34s want %d got %d", what, method, truncate(path, 34), wantStatus, resp.status))
	if !ok && !strings.Contains(resp.body, wantInBody) {
		fmt.Fprintf(r.out, "      body %q does not contain %q\n", truncate(resp.body, 200), wantInBody)
	}
	return resp, nil
}

func (r *runner) record(ok bool, line string) {
	r.checks++
	mark := "ok  "
	if !ok {
		r.failed++
		mark = "FAIL"
	}
	fmt.Fprintf(r.out, "%s  %s\n", mark, line)
}

func (r *runner) scenario() error {
	if err := r.waitReady(); err != nil {
		return err
	}
	// A per-run tag keeps reruns against a persistent database from
	// colliding with the albums an earlier run created.
	tag := runTag()
	originals := []struct {
		title, artist string
		cents         int
	}{
		{"Blue Train", "John Coltrane", 5699},
		{"Jeru", "Gerry Mulligan", 1799},
		{"Sarah Vaughan and Clifford Brown", "Sarah Vaughan", 3999},
	}
	var locations []string
	for _, o := range originals {
		body := fmt.Sprintf(`{"title":%q,"artist":%q,"price_cents":%d}`, o.title+" "+tag, o.artist, o.cents)
		resp, err := r.expect("create "+o.title, http.MethodPost, "/albums", "application/json", body, http.StatusCreated, `"id":`)
		if err != nil {
			return err
		}
		locations = append(locations, resp.header.Get("Location"))
	}
	if locations[0] == "" {
		r.record(false, "201 responses carry a Location header")
		return nil
	}
	if _, err := r.expect("get it back by Location", http.MethodGet, locations[0], "", "", http.StatusOK, "Blue Train "+tag); err != nil {
		return err
	}
	if err := r.checkPagination(locations); err != nil {
		return err
	}

	dup := fmt.Sprintf(`{"title":%q,"artist":"John Coltrane","price_cents":1}`, "Blue Train "+tag)
	errorCases := []struct {
		what, method, path, contentType, body string
		status                                int
		code                                  string
	}{
		{"same title and artist again", http.MethodPost, "/albums", "application/json", dup, 409, `"conflict"`},
		{"malformed JSON", http.MethodPost, "/albums", "application/json", `{bad`, 400, `"invalid_json"`},
		{"client-chosen id", http.MethodPost, "/albums", "application/json", `{"id":1,"title":"t","artist":"a","price_cents":1}`, 400, `unknown field \"id\"`},
		{"empty title and negative price", http.MethodPost, "/albums", "application/json", `{"title":" ","artist":"a","price_cents":-5}`, 400, `"validation_failed"`},
		{"wrong Content-Type", http.MethodPost, "/albums", "text/plain", "hello", 415, `"unsupported_media_type"`},
		{"body over the size limit", http.MethodPost, "/albums", "application/json", `{"title":"` + strings.Repeat("x", 20<<10) + `"}`, 413, `"payload_too_large"`},
		{"album that does not exist", http.MethodGet, "/albums/999999999999", "", "", 404, `"not_found"`},
		{"id that is not a number", http.MethodGet, "/albums/abc", "", "", 400, `"invalid_id"`},
		{"method the route lacks", http.MethodDelete, "/albums", "", "", 405, `"method_not_allowed"`},
		{"route that does not exist", http.MethodGet, "/nope", "", "", 404, `"not_found"`},
		{"liveness probe", http.MethodGet, "/healthz", "", "", 200, `"ok"`},
	}
	for _, c := range errorCases {
		if _, err := r.expect(c.what, c.method, c.path, c.contentType, c.body, c.status, c.code); err != nil {
			return err
		}
	}
	return nil
}

// checkPagination pages from just before the first new album, two at a time,
// and checks the three new albums come back in order.
func (r *runner) checkPagination(locations []string) error {
	var firstID int64
	if _, err := fmt.Sscanf(locations[0], "/albums/%d", &firstID); err != nil {
		r.record(false, fmt.Sprintf("Location %q has the form /albums/{id}", locations[0]))
		return nil
	}
	var got []string
	path := fmt.Sprintf("/albums?after_id=%d&limit=2", firstID-1)
	sawNext := false
	for range 5 {
		resp, err := r.send(http.MethodGet, path, "", "")
		if err != nil {
			return fmt.Errorf("list: %w", err)
		}
		page, next := parsePage(resp.body)
		got = append(got, page...)
		if next == "" || len(got) >= len(locations) {
			break
		}
		sawNext = true
		path = "/albums?limit=2&after_id=" + next
	}
	if len(got) > len(locations) {
		got = got[:len(locations)]
	}
	want := make([]string, len(locations))
	for i, l := range locations {
		want[i] = strings.TrimPrefix(l, "/albums/")
	}
	ok := sawNext && strings.Join(got, ",") == strings.Join(want, ",")
	r.record(ok, fmt.Sprintf("%-38s %-7s %-34s want ids %v got %v", "page through them, two at a time", "GET", "/albums?after_id=…&limit=2", want, got))
	return nil
}

// parsePage pulls the album IDs and next_after_id out of a list response.
// It deliberately scans the JSON text rather than sharing the server's
// types: the client checks the wire format, not the server's structs.
func parsePage(body string) ([]string, string) {
	var ids []string
	rest := body
	for {
		i := strings.Index(rest, `{"id":`)
		if i < 0 {
			break
		}
		rest = rest[i+len(`{"id":`):]
		j := strings.IndexAny(rest, ",}")
		if j < 0 {
			break
		}
		ids = append(ids, rest[:j])
	}
	next := ""
	if i := strings.Index(body, `"next_after_id":`); i >= 0 {
		tail := body[i+len(`"next_after_id":`):]
		if j := strings.IndexAny(tail, ",}"); j >= 0 {
			next = tail[:j]
		}
	}
	return ids, next
}

// waitReady polls /readyz until the API and its database answer. Polling
// here observes an external system starting up; it synchronizes nothing.
func (r *runner) waitReady() error {
	for {
		resp, err := r.send(http.MethodGet, "/readyz", "", "")
		if err == nil && resp.status == http.StatusOK {
			r.record(true, fmt.Sprintf("%-38s %-7s %-34s want 200 got 200", "readiness probe", "GET", "/readyz"))
			return nil
		}
		select {
		case <-r.ctx.Done():
			return fmt.Errorf("API at %s never became ready: %w", r.base, context.Cause(r.ctx))
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// checkShutdown stops the server the client started and checks that it
// drained and exited with 143 (128 + SIGTERM).
func (r *runner) checkShutdown(s *server) {
	if err := s.cmd.Process.Signal(syscall.SIGTERM); err != nil {
		r.record(false, "send SIGTERM to the server: "+err.Error())
		return
	}
	select {
	case <-s.exited:
	case <-time.After(30 * time.Second):
		r.record(false, "server exits within 30s of SIGTERM")
		return
	}
	code := s.cmd.ProcessState.ExitCode()
	r.record(code == 143, fmt.Sprintf("%-38s %-7s %-34s want exit 143 got %d", "server drains and exits on SIGTERM", "SIGTERM", "", code))
}

func runTag() string {
	b := make([]byte, 3)
	_, _ = rand.Read(b)
	return "[run " + hex.EncodeToString(b) + "]"
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}
