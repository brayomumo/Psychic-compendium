// Package api is the HTTP layer: routing, JSON in and out, status codes and
// error responses. It knows nothing about how albums are stored; it talks to
// a Store.
//
// Routing uses the standard library's http.ServeMux, which since Go 1.22
// matches methods and path wildcards ("GET /albums/{id}"). That covers what
// the first version used Gin for.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"time"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/album"
)

// Defaults and limits for Config and query parameters.
const (
	DefaultRequestTimeout = 2 * time.Second
	DefaultMaxBodyBytes   = 16 << 10 // an album is well under 1 KiB
	DefaultPageLimit      = 20
	MaxPageLimit          = 100
	readyTimeout          = time.Second
)

// Store persists albums. It is declared here, by its consumer, so handler
// tests can use the in-memory store or a fake.
type Store interface {
	// Create stores d and returns it with its ID; album.ErrConflict if an
	// album with the same title and artist exists.
	Create(ctx context.Context, d album.Draft) (album.Album, error)
	// Get returns the album with id, or album.ErrNotFound.
	Get(ctx context.Context, id int64) (album.Album, error)
	// List returns up to limit albums with IDs above afterID, in ID order.
	List(ctx context.Context, afterID int64, limit int) ([]album.Album, error)
	// Ping reports whether the store can serve requests.
	Ping(ctx context.Context) error
}

// Config configures the handler. Zero values take the defaults above.
type Config struct {
	Store  Store        // required
	Logger *slog.Logger // nil discards logs
	// RequestTimeout bounds each store call, so a slow database turns into
	// a 503 instead of a request that hangs until the client gives up.
	RequestTimeout time.Duration
	// MaxBodyBytes caps request bodies; larger ones get 413.
	MaxBodyBytes int64
}

type handler struct {
	store   Store
	log     *slog.Logger
	timeout time.Duration
	maxBody int64
}

// New returns the API as an http.Handler, with middleware applied. It
// panics if cfg.Store is nil, which is a programming error.
func New(cfg Config) http.Handler {
	if cfg.Store == nil {
		panic("api: Config.Store is nil")
	}
	h := &handler{store: cfg.Store, log: cfg.Logger, timeout: cfg.RequestTimeout, maxBody: cfg.MaxBodyBytes}
	if h.log == nil {
		h.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if h.timeout <= 0 {
		h.timeout = DefaultRequestTimeout
	}
	if h.maxBody <= 0 {
		h.maxBody = DefaultMaxBodyBytes
	}

	mux := http.NewServeMux()
	// A pattern with a method is more specific than the same pattern
	// without one, so each method-less pattern below only catches the
	// methods the route does not support, and answers with a JSON 405 and an
	// Allow header instead of ServeMux's plain-text one. GET patterns also
	// match HEAD.
	mux.HandleFunc("GET /albums", h.listAlbums)
	mux.HandleFunc("POST /albums", h.createAlbum)
	mux.HandleFunc("/albums", methodNotAllowed("GET, HEAD, POST"))
	mux.HandleFunc("GET /albums/{id}", h.getAlbum)
	mux.HandleFunc("/albums/{id}", methodNotAllowed("GET, HEAD"))
	mux.HandleFunc("GET /healthz", healthz)
	mux.HandleFunc("/healthz", methodNotAllowed("GET, HEAD"))
	mux.HandleFunc("GET /readyz", h.readyz)
	mux.HandleFunc("/readyz", methodNotAllowed("GET, HEAD"))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeError(w, r, http.StatusNotFound, "not_found", "no such route", nil)
	})
	return instrument(h.log, mux)
}

// albumJSON is an album as the API presents it.
type albumJSON struct {
	ID         int64     `json:"id"`
	Title      string    `json:"title"`
	Artist     string    `json:"artist"`
	PriceCents int64     `json:"price_cents"`
	CreatedAt  time.Time `json:"created_at"`
}

func toJSON(a album.Album) albumJSON {
	return albumJSON{ID: a.ID, Title: a.Title, Artist: a.Artist, PriceCents: a.PriceCents, CreatedAt: a.CreatedAt}
}

// createRequest is the body of POST /albums. There is deliberately no id:
// the server assigns IDs, and an "id" field is rejected as unknown.
type createRequest struct {
	Title      string `json:"title"`
	Artist     string `json:"artist"`
	PriceCents *int64 `json:"price_cents"` // pointer: missing is not zero
}

type listResponse struct {
	Albums []albumJSON `json:"albums"`
	// NextAfterID is the after_id for the next page; absent on the last.
	NextAfterID *int64 `json:"next_after_id,omitempty"`
}

func (h *handler) createAlbum(w http.ResponseWriter, r *http.Request) {
	if !isJSON(r.Header.Get("Content-Type")) {
		writeError(w, r, http.StatusUnsupportedMediaType, "unsupported_media_type",
			"Content-Type must be application/json", nil)
		return
	}
	var req createRequest
	if status, code, msg := decodeJSON(w, r, h.maxBody, &req); status != 0 {
		writeError(w, r, status, code, msg, nil)
		return
	}
	draft, err := album.NewDraft(album.Input{Title: req.Title, Artist: req.Artist, PriceCents: req.PriceCents})
	if err != nil {
		var verr *album.ValidationError
		if errors.As(err, &verr) {
			writeError(w, r, http.StatusBadRequest, "validation_failed", "the album is invalid", verr.Fields)
			return
		}
		h.internalError(w, r, err)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), h.timeout)
	defer cancel()
	a, err := h.store.Create(ctx, draft)
	switch {
	case errors.Is(err, album.ErrConflict):
		writeError(w, r, http.StatusConflict, "conflict",
			"an album with this title and artist already exists", nil)
	case err != nil:
		h.storeError(w, r, err)
	default:
		w.Header().Set("Location", "/albums/"+strconv.FormatInt(a.ID, 10))
		writeJSON(w, http.StatusCreated, toJSON(a))
	}
}

func (h *handler) getAlbum(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id < 1 {
		writeError(w, r, http.StatusBadRequest, "invalid_id", "album id must be a positive integer", nil)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), h.timeout)
	defer cancel()
	a, err := h.store.Get(ctx, id)
	switch {
	case errors.Is(err, album.ErrNotFound):
		writeError(w, r, http.StatusNotFound, "not_found", fmt.Sprintf("album %d does not exist", id), nil)
	case err != nil:
		h.storeError(w, r, err)
	default:
		writeJSON(w, http.StatusOK, toJSON(a))
	}
}

func (h *handler) listAlbums(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var fields []album.FieldError
	limit, ok := intParam(q.Get("limit"), DefaultPageLimit, 1, MaxPageLimit)
	if !ok {
		fields = append(fields, album.FieldError{
			Field: "limit", Message: fmt.Sprintf("must be an integer between 1 and %d", MaxPageLimit),
		})
	}
	afterID, ok := intParam(q.Get("after_id"), 0, 0, 1<<62)
	if !ok {
		fields = append(fields, album.FieldError{Field: "after_id", Message: "must be a non-negative integer"})
	}
	if len(fields) > 0 {
		writeError(w, r, http.StatusBadRequest, "invalid_query", "invalid query parameters", fields)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), h.timeout)
	defer cancel()
	// One extra row tells us whether another page exists without a COUNT.
	albums, err := h.store.List(ctx, afterID, int(limit)+1)
	if err != nil {
		h.storeError(w, r, err)
		return
	}
	resp := listResponse{Albums: make([]albumJSON, 0, min(len(albums), int(limit)))}
	if len(albums) > int(limit) {
		albums = albums[:limit]
		next := albums[len(albums)-1].ID
		resp.NextAfterID = &next
	}
	for _, a := range albums {
		resp.Albums = append(resp.Albums, toJSON(a))
	}
	writeJSON(w, http.StatusOK, resp)
}

// healthz is the liveness probe: the process is up and serving. It never
// touches the database, so a database outage does not get the process
// restarted.
func healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// readyz is the readiness probe: the API can serve requests right now,
// which means the store answers.
func (h *handler) readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), min(h.timeout, readyTimeout))
	defer cancel()
	if err := h.store.Ping(ctx); err != nil {
		h.log.WarnContext(r.Context(), "readiness check failed", "request_id", requestID(r.Context()), "error", err)
		writeError(w, r, http.StatusServiceUnavailable, "not_ready", "the database is not reachable", nil)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func methodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Allow", allow)
		writeError(w, r, http.StatusMethodNotAllowed, "method_not_allowed",
			fmt.Sprintf("%s is not allowed here; use %s", r.Method, allow), nil)
	}
}

// storeError answers a failed store call without leaking its details.
func (h *handler) storeError(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case r.Context().Err() != nil:
		// The client went away; nobody is left to answer.
		h.log.InfoContext(r.Context(), "client disconnected", "request_id", requestID(r.Context()))
	case errors.Is(err, context.DeadlineExceeded):
		h.log.WarnContext(r.Context(), "store timed out", "request_id", requestID(r.Context()), "error", err)
		w.Header().Set("Retry-After", "1")
		writeError(w, r, http.StatusServiceUnavailable, "unavailable",
			"the database did not respond in time; retry shortly", nil)
	default:
		h.internalError(w, r, err)
	}
}

func (h *handler) internalError(w http.ResponseWriter, r *http.Request, err error) {
	h.log.ErrorContext(r.Context(), "internal error", "request_id", requestID(r.Context()), "error", err)
	writeError(w, r, http.StatusInternalServerError, "internal", "internal server error", nil)
}

// isJSON reports whether a Content-Type header names JSON. Parameters such
// as charset=utf-8 are allowed.
func isJSON(contentType string) bool {
	mediaType, _, err := mime.ParseMediaType(contentType)
	return err == nil && mediaType == "application/json"
}

// decodeJSON decodes exactly one JSON object from the body into dst. On
// failure it returns the status, error code and a message that is safe to
// show the client; status is 0 on success.
func decodeJSON(w http.ResponseWriter, r *http.Request, maxBytes int64, dst any) (int, string, string) {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBytes))
	dec.DisallowUnknownFields()
	err := dec.Decode(dst)
	if err == nil {
		// A second value ({"a":1}{"b":2}) or trailing garbage is an error too.
		if dec.Decode(&struct{}{}) != io.EOF {
			return http.StatusBadRequest, "invalid_json", "request body must contain a single JSON object"
		}
		return 0, "", ""
	}

	var (
		maxErr    *http.MaxBytesError
		syntaxErr *json.SyntaxError
		typeErr   *json.UnmarshalTypeError
	)
	switch {
	case errors.As(err, &maxErr):
		return http.StatusRequestEntityTooLarge, "payload_too_large",
			fmt.Sprintf("request body must be at most %d bytes", maxErr.Limit)
	case errors.Is(err, io.EOF):
		return http.StatusBadRequest, "invalid_json", "request body is empty"
	case errors.Is(err, io.ErrUnexpectedEOF):
		return http.StatusBadRequest, "invalid_json", "request body is truncated JSON"
	case errors.As(err, &syntaxErr):
		return http.StatusBadRequest, "invalid_json",
			fmt.Sprintf("malformed JSON at byte %d", syntaxErr.Offset)
	case errors.As(err, &typeErr):
		if typeErr.Field == "" {
			return http.StatusBadRequest, "invalid_json", "request body must be a JSON object"
		}
		return http.StatusBadRequest, "invalid_json",
			fmt.Sprintf("field %q must be %s", typeErr.Field, describeType(typeErr.Type))
	case strings.HasPrefix(err.Error(), "json: unknown field "):
		// encoding/json has no typed error for this; the field name is
		// quoted in the message.
		return http.StatusBadRequest, "invalid_json",
			strings.TrimPrefix(err.Error(), "json: ") + " (ids are assigned by the server)"
	default:
		return http.StatusBadRequest, "invalid_json", "request body is not valid JSON"
	}
}

func describeType(t reflect.Type) string {
	switch t.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return "an integer"
	case reflect.String:
		return "a string"
	default:
		return "a " + t.Kind().String()
	}
}

// intParam parses an optional integer query parameter within [lo, hi].
func intParam(raw string, def, lo, hi int64) (int64, bool) {
	if raw == "" {
		return def, true
	}
	v, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || v < lo || v > hi {
		return 0, false
	}
	return v, true
}

type errorBody struct {
	Error errorDetail `json:"error"`
}

type errorDetail struct {
	Code      string      `json:"code"`
	Message   string      `json:"message"`
	Fields    []fieldJSON `json:"fields,omitempty"`
	RequestID string      `json:"request_id,omitempty"`
}

// fieldJSON is an album.FieldError on the wire. The domain type carries no
// JSON tags, so the wire format can change without touching the domain.
type fieldJSON struct {
	Field   string `json:"field"`
	Message string `json:"message"`
}

// writeError writes the API's one error shape:
//
//	{"error": {"code": "...", "message": "...", "fields": [...], "request_id": "..."}}
//
// code is stable and meant for programs; message is for people.
func writeError(w http.ResponseWriter, r *http.Request, status int, code, msg string, fields []album.FieldError) {
	detail := errorDetail{Code: code, Message: msg, RequestID: requestID(r.Context())}
	for _, f := range fields {
		detail.Fields = append(detail.Fields, fieldJSON(f))
	}
	writeJSON(w, status, errorBody{Error: detail})
}

// writeJSON writes v as compact JSON. (The first version pretty-printed every
// response, which costs bandwidth for every client and helps no program.)
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	// An encoding error here means the client went away mid-response, and
	// the status line has already been sent; there is nothing to report to.
	_ = json.NewEncoder(w).Encode(v)
}
