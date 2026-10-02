package store

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/task"
)

var t0 = time.Date(2026, 10, 2, 15, 47, 0, 0, time.UTC)

// sampleList has a completed task, a pending one with no description, and a
// deleted ID, so a round trip exercises every field and the ID counter.
func sampleList(t *testing.T) *task.List {
	t.Helper()
	l := task.New()
	steps := []error{
		second(l.Add("Buy milk", "2 litres", t0)),
		second(l.Add("Deleted", "", t0)),
		second(l.Add("Write README", "", t0)),
		second(l.Complete(1, t0.Add(time.Hour))),
		second(l.Delete(2)),
	}
	for _, err := range steps {
		if err != nil {
			t.Fatal(err)
		}
	}
	return l
}

func second[T any](_ T, err error) error { return err }

func assertSameList(t *testing.T, got, want *task.List) {
	t.Helper()
	if got.NextID() != want.NextID() {
		t.Errorf("NextID() = %d, want %d", got.NextID(), want.NextID())
	}
	g, w := got.All(), want.All()
	if len(g) != len(w) {
		t.Fatalf("got %d tasks, want %d", len(g), len(w))
	}
	for i := range g {
		// time.Time must be compared with Equal, so compare field by field.
		if g[i].ID != w[i].ID || g[i].Name != w[i].Name || g[i].Description != w[i].Description ||
			!g[i].CreatedAt.Equal(w[i].CreatedAt) || !g[i].CompletedAt.Equal(w[i].CompletedAt) {
			t.Errorf("task %d = %+v, want %+v", i, g[i], w[i])
		}
	}
}

// tempFiles lists leftover temporary files from writeAtomic in dir.
func tempFiles(t *testing.T, dir string) []string {
	t.Helper()
	matches, err := filepath.Glob(filepath.Join(dir, ".*.tmp-*"))
	if err != nil {
		t.Fatal(err)
	}
	return matches
}

func TestLoadMissingFileIsEmptyList(t *testing.T) {
	l, err := NewFile(filepath.Join(t.TempDir(), "tasks.json")).Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	assertSameList(t, l, task.New())
}

func TestSaveLoadRoundTrip(t *testing.T) {
	f := NewFile(filepath.Join(t.TempDir(), "tasks.json"))
	want := sampleList(t)
	if err := f.Save(want); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	got, err := f.Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	assertSameList(t, got, want)
}

// The format is a contract with every file already on disk, so pin it.
func TestSaveWritesStableFormat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	if err := NewFile(path).Save(sampleList(t)); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	want := `{
  "version": 1,
  "next_id": 4,
  "tasks": [
    {
      "id": 1,
      "name": "Buy milk",
      "description": "2 litres",
      "created_at": "2026-10-02T15:47:00Z",
      "completed_at": "2026-10-02T16:47:00Z"
    },
    {
      "id": 3,
      "name": "Write README",
      "created_at": "2026-10-02T15:47:00Z"
    }
  ]
}
`
	if string(got) != want {
		t.Errorf("file contents:\n%s\nwant:\n%s", got, want)
	}
}

func TestSaveKeepsTextReadable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	l := task.New()
	if _, err := l.Add("Fix <div> & co", "naïve café", t0); err != nil {
		t.Fatal(err)
	}
	if err := NewFile(path).Save(l); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"Fix <div> & co"`, `"naïve café"`} {
		if !bytes.Contains(got, []byte(want)) {
			t.Errorf("file does not contain %s verbatim:\n%s", want, got)
		}
	}
}

func TestSaveEmptyListWritesEmptyArray(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	if err := NewFile(path).Save(task.New()); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(got, []byte(`"tasks": []`)) {
		t.Errorf("empty list encoded as:\n%s\nwant \"tasks\": []", got)
	}
}

func TestSaveCreatesPrivateFileAndDirectory(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "todo")
	path := filepath.Join(dir, "tasks.json")
	if err := NewFile(path).Save(task.New()); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	for p, want := range map[string]os.FileMode{path: 0o600, dir: 0o700 | os.ModeDir} {
		info, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		if got := info.Mode(); got != want {
			t.Errorf("mode of %s = %v, want %v", p, got, want)
		}
	}
}

func TestSaveTightensExistingFileMode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tasks.json")
	if err := os.WriteFile(path, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := NewFile(path).Save(task.New()); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Errorf("mode = %v, want 0600", got)
	}
}

func TestSaveFollowsSymlink(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target.json")
	link := filepath.Join(dir, "tasks.json")
	if err := NewFile(target).Save(task.New()); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	want := sampleList(t)
	if err := NewFile(link).Save(want); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	if info, err := os.Lstat(link); err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("Save replaced the symlink with a regular file (err=%v)", err)
	}
	got, err := NewFile(target).Load()
	if err != nil {
		t.Fatal(err)
	}
	assertSameList(t, got, want)
}

// Fails before anything is written: the temp file cannot be created.
func TestFailedSaveLeavesPreviousFileIntact(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root ignores directory permissions")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "tasks.json")
	f := NewFile(path)
	if err := f.Save(sampleList(t)); err != nil {
		t.Fatal(err)
	}
	before := snapshot(t, path)
	if err := os.Chmod(dir, 0o500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })

	if err := f.Save(task.New()); err == nil {
		t.Fatal("Save() succeeded in a read-only directory, want error")
	}
	if after := snapshot(t, path); after != before {
		t.Errorf("file changed after failed save:\n%s\nwant:\n%s", after, before)
	}
	if leftovers := tempFiles(t, dir); len(leftovers) > 0 {
		t.Errorf("temporary files left behind: %v", leftovers)
	}
}

// Fails at the last step: the temp file is complete but cannot be renamed
// over a non-empty directory. The temp file must not be left behind.
func TestFailedRenameRemovesTempFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "tasks.json")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, "keep"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := NewFile(path).Save(sampleList(t)); err == nil {
		t.Fatal("Save() over a directory succeeded, want error")
	}
	if leftovers := tempFiles(t, dir); len(leftovers) > 0 {
		t.Errorf("temporary files left behind: %v", leftovers)
	}
	if _, err := os.Stat(filepath.Join(path, "keep")); err != nil {
		t.Errorf("directory contents disturbed: %v", err)
	}
}

// snapshot returns the file contents.
func snapshot(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestLoadRejectsCorruptFileWithoutModifyingIt(t *testing.T) {
	tests := []struct {
		name, contents, wantMsg string
	}{
		{"empty file", "", "EOF"},
		{"not JSON", "buy milk\n", "invalid character"},
		{"truncated", `{"version": 1, "next_id": 2, "tasks": [{"id": 1`, "unexpected EOF"},
		{"null", "null", "unsupported format version 0"},
		{"missing version", `{"next_id": 1, "tasks": []}`, "unsupported format version 0"},
		{"future version", `{"version": 2, "next_id": 1, "tasks": []}`, "unsupported format version 2"},
		{"unknown field", `{"version": 1, "next_id": 2, "tasks": [{"id": 1, "name": "a", "created_at": "2026-10-02T15:47:00Z", "complted_at": "2026-10-02T15:47:00Z"}]}`, `unknown field "complted_at"`},
		{"trailing data", `{"version": 1, "next_id": 1, "tasks": []} {}`, "unexpected data after"},
		{"wrong type", `{"version": 1, "next_id": "two", "tasks": []}`, "cannot unmarshal"},
		{"bad timestamp", `{"version": 1, "next_id": 2, "tasks": [{"id": 1, "name": "a", "created_at": "yesterday"}]}`, "parsing time"},
		{"duplicate ID", `{"version": 1, "next_id": 3, "tasks": [{"id": 1, "name": "a", "created_at": "2026-10-02T15:47:00Z"}, {"id": 1, "name": "b", "created_at": "2026-10-02T15:47:00Z"}]}`, "duplicate task ID 1"},
		{"ID not below next_id", `{"version": 1, "next_id": 1, "tasks": [{"id": 1, "name": "a", "created_at": "2026-10-02T15:47:00Z"}]}`, "not below next ID"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "tasks.json")
			if err := os.WriteFile(path, []byte(tc.contents), 0o600); err != nil {
				t.Fatal(err)
			}
			_, err := NewFile(path).Load()
			if !errors.Is(err, ErrCorrupt) {
				t.Fatalf("Load() error = %v, want %v", err, ErrCorrupt)
			}
			if !strings.Contains(err.Error(), tc.wantMsg) || !strings.Contains(err.Error(), path) {
				t.Errorf("Load() error = %q, want it to name the file and contain %q", err, tc.wantMsg)
			}
			if got := snapshot(t, path); got != tc.contents {
				t.Errorf("Load modified the corrupt file: %q", got)
			}
		})
	}
}

func TestLoadUnreadableFileIsNotReportedAsCorrupt(t *testing.T) {
	// A directory at the path: reading fails for a reason that is not the
	// file's contents, so the error must not tell the user to repair it.
	path := t.TempDir()
	_, err := NewFile(path).Load()
	if err == nil || errors.Is(err, ErrCorrupt) {
		t.Errorf("Load() error = %v, want a read error that is not %v", err, ErrCorrupt)
	}
}

func TestDecodeAcceptsHandEditedFile(t *testing.T) {
	// Reordered tasks, other key order, an explicit offset and extra
	// whitespace are all fine: only meaning is validated, not formatting.
	in := `
	{"tasks": [
	   {"created_at": "2026-10-02T18:47:00+03:00", "name": "b", "id": 5},
	   {"id": 2, "name": "a", "created_at": "2026-10-02T15:47:00Z", "completed_at": null}
	 ], "next_id": 6, "version": 1}
	`
	l, err := decode([]byte(in))
	if err != nil {
		t.Fatalf("decode() error = %v", err)
	}
	got := l.All()
	if ids := []int{got[0].ID, got[1].ID}; !reflect.DeepEqual(ids, []int{2, 5}) {
		t.Errorf("IDs = %v, want [2 5]", ids)
	}
	if !got[1].CreatedAt.Equal(t0) || got[0].Done() {
		t.Errorf("decoded tasks = %+v", got)
	}
}
