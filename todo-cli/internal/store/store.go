// Package store persists a task list as a single human-readable JSON file.
//
// Every save rewrites the whole file atomically: the new contents go to a
// temporary file in the same directory, are flushed to disk, and are then
// renamed over the old file. A crash or a failed write at any point leaves
// either the complete old file or the complete new one, never a mix.
package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/task"
)

// formatVersion is bumped whenever the file layout changes incompatibly, so
// an old binary refuses a newer file instead of misreading it.
const formatVersion = 1

// ErrCorrupt reports a data file that exists but cannot be trusted. Load
// never modifies the file, so nothing is lost; the user decides whether to
// repair it or move it aside.
var ErrCorrupt = errors.New("corrupt data file")

// File stores a task list at a fixed path.
//
// It does no locking: two processes saving the same file concurrently each
// write a complete file and the last rename wins.
type File struct {
	path string
}

// NewFile returns a store for the file at path. The file and its parent
// directory are created on the first Save.
func NewFile(path string) *File { return &File{path: path} }

// Path returns the path the store was created with.
func (f *File) Path() string { return f.path }

// document is the on-disk layout. It is separate from task.Task so the
// domain type carries no serialization concerns and the file format can be
// versioned independently.
type document struct {
	Version int      `json:"version"`
	NextID  int      `json:"next_id"`
	Tasks   []record `json:"tasks"`
}

type record struct {
	ID          int        `json:"id"`
	Name        string     `json:"name"`
	Description string     `json:"description,omitempty"`
	CreatedAt   time.Time  `json:"created_at"`
	CompletedAt *time.Time `json:"completed_at,omitempty"`
}

// Load reads the task list. A missing file is an empty list, because that is
// the state before the first save. Anything else that cannot be read or
// trusted is an error; Load never guesses.
func (f *File) Load() (*task.List, error) {
	data, err := os.ReadFile(f.path)
	if errors.Is(err, fs.ErrNotExist) {
		return task.New(), nil
	}
	if err != nil {
		return nil, fmt.Errorf("read tasks: %w", err)
	}
	l, err := decode(data)
	if err != nil {
		return nil, fmt.Errorf("%w %s: %w", ErrCorrupt, f.path, err)
	}
	return l, nil
}

// Save replaces the stored list with l. The file is written with mode 0600
// because a todo list is personal; any previous mode is not preserved.
func (f *File) Save(l *task.List) error {
	data, err := encode(l)
	if err != nil {
		return fmt.Errorf("encode tasks: %w", err)
	}
	target, err := resolveSymlink(f.path)
	if err != nil {
		return err
	}
	if err := writeAtomic(target, data); err != nil {
		return fmt.Errorf("save tasks to %s: %w", target, err)
	}
	return nil
}

func decode(data []byte) (*task.List, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	// Unknown fields within a known version are typos from hand-editing
	// ("complted_at"); silently dropping them would lose data on next save.
	dec.DisallowUnknownFields()
	var doc document
	if err := dec.Decode(&doc); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("unexpected data after the JSON document")
	}
	if doc.Version != formatVersion {
		return nil, fmt.Errorf("unsupported format version %d, this build reads version %d", doc.Version, formatVersion)
	}
	tasks := make([]task.Task, len(doc.Tasks))
	for i, r := range doc.Tasks {
		tasks[i] = task.Task{ID: r.ID, Name: r.Name, Description: r.Description, CreatedAt: r.CreatedAt}
		if r.CompletedAt != nil {
			tasks[i].CompletedAt = *r.CompletedAt
		}
	}
	return task.Restore(doc.NextID, tasks)
}

func encode(l *task.List) ([]byte, error) {
	all := l.All()
	// A non-nil empty slice encodes as [] rather than null, so the file
	// always has the same shape.
	doc := document{Version: formatVersion, NextID: l.NextID(), Tasks: make([]record, 0, len(all))}
	for _, t := range all {
		r := record{ID: t.ID, Name: t.Name, Description: t.Description, CreatedAt: t.CreatedAt}
		if t.Done() {
			completed := t.CompletedAt
			r.CompletedAt = &completed
		}
		doc.Tasks = append(doc.Tasks, r)
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetIndent("", "  ")
	// The file is meant to be read by people; "<b> & co" should not become
	// "<b> & co". Escaping only matters for embedding in HTML.
	enc.SetEscapeHTML(false)
	if err := enc.Encode(doc); err != nil { // Encode appends the final newline
		return nil, err
	}
	return buf.Bytes(), nil
}

// resolveSymlink follows a symlink at path, so saving updates the file it
// points to instead of replacing the link with a regular file (a common way
// to break dotfile setups with rename-based writes).
func resolveSymlink(path string) (string, error) {
	target, err := filepath.EvalSymlinks(path)
	switch {
	case err == nil:
		return target, nil
	case errors.Is(err, fs.ErrNotExist):
		return path, nil
	default:
		return "", fmt.Errorf("resolve %s: %w", path, err)
	}
}

// writeAtomic replaces path with data so that readers and crashes observe
// either the old contents or the new, never a partial file.
func writeAtomic(path string, data []byte) (err error) {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	// Same directory as the target: rename is only atomic within a
	// filesystem. CreateTemp opens the file with mode 0600.
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	renamed := false
	defer func() {
		if !renamed {
			// Best effort cleanup; the original file is untouched either way.
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		return err
	}
	// Flush the contents before the rename makes them visible; otherwise a
	// power loss can leave a renamed but empty file.
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		return err
	}
	renamed = true
	// The rename lives in the directory entry; sync the directory so the
	// rename itself survives a power loss.
	return syncDir(dir)
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	if err := d.Sync(); err != nil {
		_ = d.Close()
		return err
	}
	return d.Close()
}
