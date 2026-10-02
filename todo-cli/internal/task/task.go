// Package task is the domain model of the todo list. It is pure: no I/O, no
// clock, no globals. Callers pass in the current time, which keeps every rule
// here deterministic and testable.
package task

import (
	"cmp"
	"errors"
	"fmt"
	"math"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// Limits on user-supplied text. They bound the size of the data file and keep
// list output readable; they are not storage constraints.
const (
	MaxNameLen        = 200
	MaxDescriptionLen = 1000
)

// Sentinel errors. Callers match them with errors.Is; the wrapped message
// carries the detail (which ID, which limit).
var (
	ErrNotFound     = errors.New("task not found")
	ErrAlreadyDone  = errors.New("task already completed")
	ErrEmptyName    = errors.New("task name is empty")
	ErrInvalidText  = errors.New("invalid text")
	ErrIDsExhausted = errors.New("no task IDs left")
)

// Task is a single todo item. It is a value type: copying a Task never
// aliases another Task's state.
type Task struct {
	ID          int
	Name        string
	Description string
	CreatedAt   time.Time
	// CompletedAt is the zero time while the task is pending. Keeping the
	// completion time as the only record of "done" means the two can never
	// disagree.
	CompletedAt time.Time
}

// Done reports whether the task has been completed.
func (t Task) Done() bool { return !t.CompletedAt.IsZero() }

// List is an ordered collection of tasks with stable IDs.
//
// IDs are assigned from a counter that only ever increases, so an ID is never
// reused, even after the task holding it is deleted. Completing or deleting a
// task never renumbers the others. The zero value is not usable; call New or
// Restore.
type List struct {
	tasks  []Task // ascending by ID, which is also creation order
	nextID int
}

// New returns an empty list whose first task will get ID 1.
func New() *List { return &List{nextID: 1} }

// Restore rebuilds a list from persisted state, enforcing the same invariants
// New and the mutating methods maintain: IDs are positive, unique and below
// nextID, and every task has a creation time and valid text. Order of the
// input does not matter.
func Restore(nextID int, tasks []Task) (*List, error) {
	if nextID < 1 {
		return nil, fmt.Errorf("next ID must be at least 1, got %d", nextID)
	}
	sorted := slices.Clone(tasks)
	slices.SortFunc(sorted, func(a, b Task) int { return cmp.Compare(a.ID, b.ID) })
	for i, t := range sorted {
		switch {
		case t.ID < 1:
			return nil, fmt.Errorf("task ID must be positive, got %d", t.ID)
		case t.ID >= nextID:
			return nil, fmt.Errorf("task ID %d is not below next ID %d", t.ID, nextID)
		case i > 0 && sorted[i-1].ID == t.ID:
			return nil, fmt.Errorf("duplicate task ID %d", t.ID)
		case t.CreatedAt.IsZero():
			return nil, fmt.Errorf("task %d has no creation time", t.ID)
		}
		if err := ValidateName(t.Name); err != nil {
			return nil, fmt.Errorf("task %d: %w", t.ID, err)
		}
		if err := ValidateDescription(t.Description); err != nil {
			return nil, fmt.Errorf("task %d: %w", t.ID, err)
		}
	}
	return &List{tasks: sorted, nextID: nextID}, nil
}

// Clone returns an independent copy, so a caller can apply a change, try to
// persist it, and discard it if persisting fails.
func (l *List) Clone() *List {
	return &List{tasks: slices.Clone(l.tasks), nextID: l.nextID}
}

// NextID returns the ID the next added task will receive.
func (l *List) NextID() int { return l.nextID }

// All returns a copy of every task, ordered by ID.
func (l *List) All() []Task { return slices.Clone(l.tasks) }

// Pending returns a copy of the tasks not yet completed, ordered by ID.
func (l *List) Pending() []Task {
	var pending []Task
	for _, t := range l.tasks {
		if !t.Done() {
			pending = append(pending, t)
		}
	}
	return pending
}

// Add appends a new pending task created at now. Surrounding whitespace in
// name and description is trimmed.
func (l *List) Add(name, description string, now time.Time) (Task, error) {
	name, description = strings.TrimSpace(name), strings.TrimSpace(description)
	if err := ValidateName(name); err != nil {
		return Task{}, err
	}
	if err := ValidateDescription(description); err != nil {
		return Task{}, err
	}
	// Only reachable with a hand-edited data file, but incrementing past
	// MaxInt would wrap to negative IDs and break every invariant above.
	if l.nextID == math.MaxInt {
		return Task{}, ErrIDsExhausted
	}
	t := Task{
		ID:          l.nextID,
		Name:        name,
		Description: description,
		CreatedAt:   now.UTC(),
	}
	l.tasks = append(l.tasks, t)
	l.nextID++
	return t, nil
}

// Complete marks the task with the given ID as done at now. Completing a task
// twice is an error rather than a silent overwrite of the first completion
// time.
func (l *List) Complete(id int, now time.Time) (Task, error) {
	i, err := l.index(id)
	if err != nil {
		return Task{}, err
	}
	if l.tasks[i].Done() {
		return Task{}, fmt.Errorf("%w: %d", ErrAlreadyDone, id)
	}
	l.tasks[i].CompletedAt = now.UTC()
	return l.tasks[i], nil
}

// Delete removes the task with the given ID and returns it.
func (l *List) Delete(id int) (Task, error) {
	i, err := l.index(id)
	if err != nil {
		return Task{}, err
	}
	t := l.tasks[i]
	l.tasks = slices.Delete(l.tasks, i, i+1)
	return t, nil
}

func (l *List) index(id int) (int, error) {
	i, found := slices.BinarySearchFunc(l.tasks, id, func(t Task, id int) int { return cmp.Compare(t.ID, id) })
	if !found {
		return 0, fmt.Errorf("%w: %d", ErrNotFound, id)
	}
	return i, nil
}

// ValidateName reports whether name, after trimming surrounding whitespace,
// is acceptable as a task name.
func ValidateName(name string) error {
	if strings.TrimSpace(name) == "" {
		return ErrEmptyName
	}
	return validateText("name", name, MaxNameLen)
}

// ValidateDescription reports whether description is acceptable. An empty
// description is allowed.
func ValidateDescription(description string) error {
	return validateText("description", description, MaxDescriptionLen)
}

// validateText rejects text that is too long, is not UTF-8, or contains
// control characters. Control characters would break column alignment and
// could smuggle terminal escape sequences into list output. It judges the
// trimmed text, because that is what Add stores.
func validateText(field, s string, maxLen int) error {
	s = strings.TrimSpace(s)
	if !utf8.ValidString(s) {
		return fmt.Errorf("%w: %s is not valid UTF-8", ErrInvalidText, field)
	}
	if n := utf8.RuneCountInString(s); n > maxLen {
		return fmt.Errorf("%w: %s is %d characters, the limit is %d", ErrInvalidText, field, n, maxLen)
	}
	if strings.IndexFunc(s, unicode.IsControl) >= 0 {
		return fmt.Errorf("%w: %s contains control characters", ErrInvalidText, field)
	}
	return nil
}
