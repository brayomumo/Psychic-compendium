package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/album"
	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/store"
)

// fakeStore wraps a real memory store and lets a test replace any method.
type fakeStore struct {
	*store.Memory
	create func(ctx context.Context, d album.Draft) (album.Album, error)
	ping   func(ctx context.Context) error
}

func (f *fakeStore) Create(ctx context.Context, d album.Draft) (album.Album, error) {
	if f.create != nil {
		return f.create(ctx, d)
	}
	return f.Memory.Create(ctx, d)
}

func (f *fakeStore) Ping(ctx context.Context) error {
	if f.ping != nil {
		return f.ping(ctx)
	}
	return f.Memory.Ping(ctx)
}

type harness struct {
	h    http.Handler
	logs *syncBuffer
}

// syncBuffer is a bytes.Buffer safe for the concurrent writes slog makes.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func newHarness(t *testing.T, s Store, timeout time.Duration) harness {
	t.Helper()
	logs := &syncBuffer{}
	if s == nil {
		s = store.NewMemory(nil)
	}
	return harness{
		h:    New(Config{Store: s, Logger: slog.New(slog.NewTextHandler(logs, nil)), RequestTimeout: timeout}),
		logs: logs,
	}
}

func (hs harness) do(method, target, body string, header ...string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	for i := 0; i+1 < len(header); i += 2 {
		if header[i+1] == "" {
			req.Header.Del(header[i])
		} else {
			req.Header.Set(header[i], header[i+1])
		}
	}
	rec := httptest.NewRecorder()
	hs.h.ServeHTTP(rec, req)
	return rec
}

func decodeErr(t *testing.T, rec *httptest.ResponseRecorder) errorDetail {
	t.Helper()
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json (body %q)", ct, rec.Body.String())
	}
	var body errorBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("error body is not JSON: %v: %q", err, rec.Body.String())
	}
	if body.Error.Code == "" || body.Error.Message == "" || body.Error.RequestID == "" {
		t.Errorf("error body %+v is missing code, message or request_id", body.Error)
	}
	return body.Error
}

const validAlbum = `{"title":"Blue Train","artist":"John Coltrane","price_cents":5699}`

func TestCreateAlbum(t *testing.T) {
	hs := newHarness(t, nil, 0)
	rec := hs.do(http.MethodPost, "/albums", validAlbum)
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body %s", rec.Code, rec.Body)
	}
	var got albumJSON
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.ID != 1 || got.Title != "Blue Train" || got.PriceCents != 5699 || got.CreatedAt.IsZero() {
		t.Errorf("created %+v", got)
	}
	if loc := rec.Header().Get("Location"); loc != "/albums/1" {
		t.Errorf("Location = %q, want /albums/1", loc)
	}
	// Regression: the first version pretty-printed every response.
	if strings.Contains(rec.Body.String(), "\n ") {
		t.Errorf("response is indented: %q", rec.Body.String())
	}
}

func TestCreateAlbumRejectsBadRequests(t *testing.T) {
	big := `{"title":"` + strings.Repeat("x", DefaultMaxBodyBytes) + `"}`
	tests := []struct {
		name        string
		body        string
		contentType string // "" keeps application/json; "-" removes the header
		wantStatus  int
		wantCode    string
		wantInMsg   string
	}{
		// Regression: the first version answered malformed JSON with a bare
		// 400, no body and no Content-Type.
		{"malformed JSON", `{bad`, "", 400, "invalid_json", "malformed JSON at byte"},
		{"empty body", ``, "application/json", 400, "invalid_json", "empty"},
		{"truncated JSON", `{"title":"x"`, "", 400, "invalid_json", "truncated"},
		{"two objects", validAlbum + validAlbum, "", 400, "invalid_json", "single JSON object"},
		{"array instead of object", `[1,2]`, "", 400, "invalid_json", "must be a JSON object"},
		{"price as a float", `{"title":"t","artist":"a","price_cents":12.5}`, "", 400, "invalid_json", `"price_cents" must be an integer`},
		{"title as a number", `{"title":7,"artist":"a","price_cents":1}`, "", 400, "invalid_json", `"title" must be a string`},
		// Regression: clients chose IDs in the first version, and could reuse them.
		{"client-supplied id", `{"id":"1","title":"t","artist":"a","price_cents":1}`, "", 400, "invalid_json", `unknown field "id"`},
		{"form-encoded", `title=t`, "application/x-www-form-urlencoded", 415, "unsupported_media_type", "application/json"},
		{"no content type", validAlbum, "-", 415, "unsupported_media_type", "application/json"},
		{"too large", big, "", 413, "payload_too_large", "at most"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			hs := newHarness(t, nil, 0)
			ct := tt.contentType
			if ct == "-" {
				ct = ""
			} else if ct == "" {
				ct = "application/json"
			}
			rec := hs.do(http.MethodPost, "/albums", tt.body, "Content-Type", ct)
			if rec.Code != tt.wantStatus {
				t.Fatalf("status = %d, want %d; body %s", rec.Code, tt.wantStatus, rec.Body)
			}
			e := decodeErr(t, rec)
			if e.Code != tt.wantCode || !strings.Contains(e.Message, tt.wantInMsg) {
				t.Errorf("error = %+v, want code %q and message containing %q", e, tt.wantCode, tt.wantInMsg)
			}
		})
	}
}

func TestCreateAlbumCharsetIsAccepted(t *testing.T) {
	hs := newHarness(t, nil, 0)
	rec := hs.do(http.MethodPost, "/albums", validAlbum, "Content-Type", "application/json; charset=utf-8")
	if rec.Code != http.StatusCreated {
		t.Errorf("status = %d, want 201; body %s", rec.Code, rec.Body)
	}
}

// Regression: the first version accepted an empty album and a negative price.
func TestCreateAlbumReportsEveryInvalidField_RegressionNoValidation(t *testing.T) {
	hs := newHarness(t, nil, 0)
	rec := hs.do(http.MethodPost, "/albums", `{"title":"  ","artist":"a","price_cents":-5}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body %s", rec.Code, rec.Body)
	}
	e := decodeErr(t, rec)
	want := []fieldJSON{{"title", "is required"}, {"price_cents", "must be between 0 and 10000000"}}
	if e.Code != "validation_failed" || fmt.Sprint(e.Fields) != fmt.Sprint(want) {
		t.Errorf("error = %+v, want validation_failed with %+v", e, want)
	}
}

func TestCreateDuplicateAlbumConflicts(t *testing.T) {
	hs := newHarness(t, nil, 0)
	hs.do(http.MethodPost, "/albums", validAlbum)
	rec := hs.do(http.MethodPost, "/albums", validAlbum)
	if rec.Code != http.StatusConflict || decodeErr(t, rec).Code != "conflict" {
		t.Errorf("status = %d, body %s; want 409 conflict", rec.Code, rec.Body)
	}
}

func TestGetAlbum(t *testing.T) {
	hs := newHarness(t, nil, 0)
	hs.do(http.MethodPost, "/albums", validAlbum)
	tests := []struct {
		target     string
		wantStatus int
		wantCode   string
	}{
		{"/albums/1", 200, ""},
		{"/albums/2", 404, "not_found"},
		{"/albums/abc", 400, "invalid_id"},
		{"/albums/0", 400, "invalid_id"},
		{"/albums/-3", 400, "invalid_id"},
		{"/albums/99999999999999999999", 400, "invalid_id"},
	}
	for _, tt := range tests {
		t.Run(tt.target, func(t *testing.T) {
			rec := hs.do(http.MethodGet, tt.target, "")
			if rec.Code != tt.wantStatus {
				t.Fatalf("status = %d, want %d; body %s", rec.Code, tt.wantStatus, rec.Body)
			}
			if tt.wantCode != "" {
				if code := decodeErr(t, rec).Code; code != tt.wantCode {
					t.Errorf("code = %q, want %q", code, tt.wantCode)
				}
				return
			}
			var got albumJSON
			if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || got.ID != 1 {
				t.Errorf("body %s, err %v; want album 1", rec.Body, err)
			}
		})
	}
}

func TestListAlbumsPaginates(t *testing.T) {
	hs := newHarness(t, nil, 0)
	empty := hs.do(http.MethodGet, "/albums", "")
	if empty.Code != 200 || strings.TrimSpace(empty.Body.String()) != `{"albums":[]}` {
		t.Errorf("empty list = %d %s, want 200 {\"albums\":[]} (never null)", empty.Code, empty.Body)
	}
	for i := range 5 {
		hs.do(http.MethodPost, "/albums", fmt.Sprintf(`{"title":"Album %d","artist":"A","price_cents":%d}`, i, i))
	}
	var ids []int64
	target := "/albums?limit=2"
	for range 10 {
		rec := hs.do(http.MethodGet, target, "")
		if rec.Code != 200 {
			t.Fatalf("GET %s = %d %s", target, rec.Code, rec.Body)
		}
		var page listResponse
		if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
			t.Fatal(err)
		}
		for _, a := range page.Albums {
			ids = append(ids, a.ID)
		}
		if page.NextAfterID == nil {
			break
		}
		target = fmt.Sprintf("/albums?limit=2&after_id=%d", *page.NextAfterID)
	}
	if fmt.Sprint(ids) != "[1 2 3 4 5]" {
		t.Errorf("paged IDs = %v, want [1 2 3 4 5]", ids)
	}
}

func TestListAlbumsRejectsBadQueries(t *testing.T) {
	hs := newHarness(t, nil, 0)
	for _, q := range []string{"limit=0", "limit=101", "limit=abc", "after_id=-1", "after_id=x", "limit=0&after_id=-1"} {
		t.Run(q, func(t *testing.T) {
			rec := hs.do(http.MethodGet, "/albums?"+q, "")
			if rec.Code != 400 || decodeErr(t, rec).Code != "invalid_query" {
				t.Errorf("status = %d, body %s; want 400 invalid_query", rec.Code, rec.Body)
			}
		})
	}
	rec := hs.do(http.MethodGet, "/albums?limit=0&after_id=-1", "")
	if n := len(decodeErr(t, rec).Fields); n != 2 {
		t.Errorf("got %d field errors, want both limit and after_id reported", n)
	}
}

func TestUnsupportedMethodsGetJSON405WithAllow(t *testing.T) {
	hs := newHarness(t, nil, 0)
	tests := []struct{ method, target, allow string }{
		{http.MethodDelete, "/albums", "GET, HEAD, POST"},
		{http.MethodPut, "/albums/1", "GET, HEAD"},
		{http.MethodPost, "/healthz", "GET, HEAD"},
		{http.MethodPatch, "/readyz", "GET, HEAD"},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.target, func(t *testing.T) {
			rec := hs.do(tt.method, tt.target, "")
			if rec.Code != http.StatusMethodNotAllowed || rec.Header().Get("Allow") != tt.allow {
				t.Fatalf("status = %d Allow = %q; want 405 and %q", rec.Code, rec.Header().Get("Allow"), tt.allow)
			}
			if decodeErr(t, rec).Code != "method_not_allowed" {
				t.Errorf("body %s", rec.Body)
			}
		})
	}
}

func TestUnknownRouteGetsJSON404(t *testing.T) {
	hs := newHarness(t, nil, 0)
	for _, target := range []string{"/", "/nope", "/albums/1/tracks", "/albums/"} {
		rec := hs.do(http.MethodGet, target, "")
		if rec.Code != 404 || decodeErr(t, rec).Code != "not_found" {
			t.Errorf("GET %s = %d %s, want JSON 404", target, rec.Code, rec.Body)
		}
	}
}

func TestHeadIsServedWithoutBody(t *testing.T) {
	srv := httptest.NewServer(newHarness(t, nil, 0).h)
	defer srv.Close()
	resp, err := http.Head(srv.URL + "/albums")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	// Content-Length may announce the size a GET would return; the body
	// itself must be empty.
	if resp.StatusCode != 200 || len(body) != 0 {
		t.Errorf("HEAD /albums = %d with %d body bytes, want 200 and no body", resp.StatusCode, len(body))
	}
}

func TestRequestIDIsKeptOrReplaced(t *testing.T) {
	hs := newHarness(t, nil, 0)
	kept := hs.do(http.MethodGet, "/albums/9", "", RequestIDHeader, "edge-proxy.42")
	if got := kept.Header().Get(RequestIDHeader); got != "edge-proxy.42" {
		t.Errorf("valid ID replaced: got %q", got)
	}
	if decodeErr(t, kept).RequestID != "edge-proxy.42" {
		t.Error("error body does not carry the request ID")
	}
	for _, bad := range []string{"has space", "new\nline", strings.Repeat("a", maxRequestIDLen+1)} {
		rec := hs.do(http.MethodGet, "/healthz", "", RequestIDHeader, bad)
		got := rec.Header().Get(RequestIDHeader)
		if got == bad || !validRequestID(got) {
			t.Errorf("unsafe ID %q was kept or replaced badly: %q", bad, got)
		}
	}
	if !strings.Contains(hs.logs.String(), "request_id=edge-proxy.42") {
		t.Errorf("log does not carry the request ID:\n%s", hs.logs)
	}
}

func TestPanicIsRecoveredAsJSON500(t *testing.T) {
	fs := &fakeStore{Memory: store.NewMemory(nil), create: func(context.Context, album.Draft) (album.Album, error) {
		panic("boom")
	}}
	hs := newHarness(t, fs, 0)
	rec := hs.do(http.MethodPost, "/albums", validAlbum)
	if rec.Code != 500 || decodeErr(t, rec).Code != "internal" {
		t.Fatalf("status = %d body %s, want JSON 500", rec.Code, rec.Body)
	}
	if strings.Contains(rec.Body.String(), "boom") {
		t.Error("panic value leaked to the client")
	}
	if !strings.Contains(hs.logs.String(), "panic serving request") {
		t.Errorf("panic not logged:\n%s", hs.logs)
	}
	// The handler keeps serving after a panic.
	if rec := hs.do(http.MethodGet, "/healthz", ""); rec.Code != 200 {
		t.Errorf("after panic, /healthz = %d", rec.Code)
	}
}

func TestSlowStoreTimesOutAs503(t *testing.T) {
	fs := &fakeStore{Memory: store.NewMemory(nil), create: func(ctx context.Context, _ album.Draft) (album.Album, error) {
		<-ctx.Done() // a database that never answers
		return album.Album{}, ctx.Err()
	}}
	hs := newHarness(t, fs, 20*time.Millisecond)
	rec := hs.do(http.MethodPost, "/albums", validAlbum)
	if rec.Code != 503 || rec.Header().Get("Retry-After") == "" || decodeErr(t, rec).Code != "unavailable" {
		t.Errorf("status = %d Retry-After = %q body %s; want 503 unavailable with Retry-After",
			rec.Code, rec.Header().Get("Retry-After"), rec.Body)
	}
}

func TestStoreFailureDoesNotLeakDetails(t *testing.T) {
	fs := &fakeStore{Memory: store.NewMemory(nil), create: func(context.Context, album.Draft) (album.Album, error) {
		return album.Album{}, errors.New("dial tcp 10.0.0.5:5432: password authentication failed for user admin")
	}}
	hs := newHarness(t, fs, 0)
	rec := hs.do(http.MethodPost, "/albums", validAlbum)
	if rec.Code != 500 || strings.Contains(rec.Body.String(), "10.0.0.5") || strings.Contains(rec.Body.String(), "admin") {
		t.Errorf("status = %d body %s; want a generic 500", rec.Code, rec.Body)
	}
	if !strings.Contains(hs.logs.String(), "password authentication failed") {
		t.Error("the real error should be logged for operators")
	}
}

func TestProbes(t *testing.T) {
	down := errors.New("connection refused")
	fs := &fakeStore{Memory: store.NewMemory(nil)}
	hs := newHarness(t, fs, 0)
	if rec := hs.do(http.MethodGet, "/healthz", ""); rec.Code != 200 {
		t.Errorf("/healthz = %d", rec.Code)
	}
	if rec := hs.do(http.MethodGet, "/readyz", ""); rec.Code != 200 {
		t.Errorf("/readyz with a healthy store = %d", rec.Code)
	}
	fs.ping = func(context.Context) error { return down }
	if rec := hs.do(http.MethodGet, "/readyz", ""); rec.Code != 503 || decodeErr(t, rec).Code != "not_ready" {
		t.Errorf("/readyz with a failing store = %d %s, want 503 not_ready", rec.Code, rec.Body)
	}
	// Liveness must not depend on the database.
	if rec := hs.do(http.MethodGet, "/healthz", ""); rec.Code != 200 {
		t.Errorf("/healthz with a failing store = %d, want 200", rec.Code)
	}
}

// Regression: concurrent POSTs raced on a shared slice in the first version.
func TestConcurrentCreatesGetDistinctIDs_RegressionDataRace(t *testing.T) {
	hs := newHarness(t, nil, 0)
	const n = 50
	var wg sync.WaitGroup
	codes := make(chan int, n)
	for i := range n {
		wg.Add(1)
		go func() {
			defer wg.Done()
			body := fmt.Sprintf(`{"title":"Album %d","artist":"A","price_cents":1}`, i)
			codes <- hs.do(http.MethodPost, "/albums", body).Code
		}()
	}
	wg.Wait()
	close(codes)
	for code := range codes {
		if code != http.StatusCreated {
			t.Errorf("concurrent POST = %d, want 201", code)
		}
	}
	rec := hs.do(http.MethodGet, fmt.Sprintf("/albums?limit=%d", MaxPageLimit), "")
	var page listResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatal(err)
	}
	if len(page.Albums) != n {
		t.Errorf("listed %d albums, want %d: writes were lost", len(page.Albums), n)
	}
}

func TestNewPanicsWithoutStore(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Error("New(Config{}) did not panic")
		}
	}()
	New(Config{})
}
