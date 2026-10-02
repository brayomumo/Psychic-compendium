package main

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/api"
	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/store"
)

func TestScenarioPassesAgainstTheRealAPI(t *testing.T) {
	srv := httptest.NewServer(api.New(api.Config{Store: store.NewMemory(nil)}))
	defer srv.Close()
	var out, errOut bytes.Buffer
	code := run([]string{"-base-url", srv.URL}, &out, &errOut)
	if code != exitOK {
		t.Fatalf("exit = %d; stdout:\n%s\nstderr:\n%s", code, out.String(), errOut.String())
	}
	if !strings.Contains(out.String(), ", 0 failed") || strings.Contains(out.String(), "FAIL") {
		t.Errorf("not every check passed:\n%s", out.String())
	}
}

// The checks must be able to fail: an API that answers 200 to everything
// has to be caught.
func TestScenarioCatchesAWrongAPI(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()
	var out bytes.Buffer
	code := run([]string{"-base-url", srv.URL}, &out, &bytes.Buffer{})
	if code != exitFailure || !strings.Contains(out.String(), "FAIL") {
		t.Errorf("exit = %d, want %d with failures; stdout:\n%s", code, exitFailure, out.String())
	}
}

func TestUnreachableAPIFailsWithinTheTimeout(t *testing.T) {
	var errOut bytes.Buffer
	code := run([]string{"-base-url", "http://127.0.0.1:1", "-timeout", "300ms"}, &bytes.Buffer{}, &errOut)
	if code != exitFailure || !strings.Contains(errOut.String(), "never became ready") {
		t.Errorf("exit = %d, stderr %q; want a readiness failure", code, errOut.String())
	}
}

func TestUsageErrors(t *testing.T) {
	for _, args := range [][]string{{"-nope"}, {"extra"}, {"-timeout", "0s"}} {
		if code := run(args, &bytes.Buffer{}, &bytes.Buffer{}); code != exitUsage {
			t.Errorf("run(%q) = %d, want %d", args, code, exitUsage)
		}
	}
}

func TestParsePage(t *testing.T) {
	ids, next := parsePage(`{"albums":[{"id":4,"title":"a"},{"id":5,"title":"b"}],"next_after_id":5}`)
	if strings.Join(ids, ",") != "4,5" || next != "5" {
		t.Errorf("parsePage = %v, %q", ids, next)
	}
	ids, next = parsePage(`{"albums":[]}`)
	if len(ids) != 0 || next != "" {
		t.Errorf("empty page = %v, %q", ids, next)
	}
}
