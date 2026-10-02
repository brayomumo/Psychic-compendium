package task

import (
	"errors"
	"math"
	"slices"
	"strings"
	"testing"
	"time"
)

var (
	t0 = time.Date(2026, 10, 2, 15, 47, 0, 0, time.UTC)
	t1 = t0.Add(time.Hour)
)

// ids returns the IDs of tasks in order, which is what most tests care about.
func ids(tasks []Task) []int {
	out := make([]int, len(tasks))
	for i, t := range tasks {
		out[i] = t.ID
	}
	return out
}

// mustAdd adds tasks named after each argument and fails the test on error.
func mustAdd(t *testing.T, l *List, names ...string) {
	t.Helper()
	for _, n := range names {
		if _, err := l.Add(n, "", t0); err != nil {
			t.Fatalf("Add(%q) = %v", n, err)
		}
	}
}

func TestAddAssignsSequentialIDsAndTrims(t *testing.T) {
	l := New()
	got, err := l.Add("  Buy milk ", " 2 litres\t", t0)
	if err != nil {
		t.Fatalf("Add() error = %v", err)
	}
	want := Task{ID: 1, Name: "Buy milk", Description: "2 litres", CreatedAt: t0}
	if got != want {
		t.Errorf("Add() = %+v, want %+v", got, want)
	}
	mustAdd(t, l, "second")
	if got, want := ids(l.All()), []int{1, 2}; !slices.Equal(got, want) {
		t.Errorf("IDs = %v, want %v", got, want)
	}
	if got := l.NextID(); got != 3 {
		t.Errorf("NextID() = %d, want 3", got)
	}
}

func TestAddStoresTimesInUTC(t *testing.T) {
	nairobi := time.FixedZone("EAT", 3*60*60)
	l := New()
	got, err := l.Add("x", "", t0.In(nairobi))
	if err != nil {
		t.Fatal(err)
	}
	if got.CreatedAt.Location() != time.UTC || !got.CreatedAt.Equal(t0) {
		t.Errorf("CreatedAt = %v, want %v in UTC", got.CreatedAt, t0)
	}
}

func TestAddRejectsInvalidText(t *testing.T) {
	tests := []struct {
		name, taskName, desc string
		want                 error
	}{
		{"empty name", "", "", ErrEmptyName},
		{"whitespace name", " \t ", "", ErrEmptyName},
		{"name too long", strings.Repeat("a", MaxNameLen+1), "", ErrInvalidText},
		{"description too long", "ok", strings.Repeat("a", MaxDescriptionLen+1), ErrInvalidText},
		{"control char in name", "a\x1b[31mred", "", ErrInvalidText},
		{"tab inside description", "ok", "a\tb", ErrInvalidText},
		{"invalid UTF-8", "\xff", "", ErrInvalidText},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			l := New()
			if _, err := l.Add(tc.taskName, tc.desc, t0); !errors.Is(err, tc.want) {
				t.Errorf("Add() error = %v, want %v", err, tc.want)
			}
			if len(l.All()) != 0 || l.NextID() != 1 {
				t.Errorf("failed Add changed the list: %v, next ID %d", l.All(), l.NextID())
			}
		})
	}
}

func TestAddAcceptsTextAtTheLimit(t *testing.T) {
	// Limits count characters, not bytes: 200 multi-byte runes is fine.
	name := strings.Repeat("é", MaxNameLen)
	if _, err := New().Add(name, strings.Repeat("ü", MaxDescriptionLen), t0); err != nil {
		t.Errorf("Add() at the limit error = %v", err)
	}
}

func TestAddFailsWhenIDsAreExhausted(t *testing.T) {
	l, err := Restore(math.MaxInt, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := l.Add("x", "", t0); !errors.Is(err, ErrIDsExhausted) {
		t.Errorf("Add() error = %v, want %v", err, ErrIDsExhausted)
	}
}

func TestComplete(t *testing.T) {
	l := New()
	mustAdd(t, l, "a", "b")

	got, err := l.Complete(2, t1)
	if err != nil {
		t.Fatalf("Complete(2) error = %v", err)
	}
	if !got.Done() || !got.CompletedAt.Equal(t1) {
		t.Errorf("Complete(2) = %+v, want done at %v", got, t1)
	}
	if got, want := ids(l.Pending()), []int{1}; !slices.Equal(got, want) {
		t.Errorf("Pending() IDs = %v, want %v", got, want)
	}
}

// The first version removed the task and re-appended it, so completing task 1
// of 3 moved it to the end and every number the user saw changed.
func TestCompleteKeepsOrderAndIDs(t *testing.T) {
	l := New()
	mustAdd(t, l, "a", "b", "c")
	if _, err := l.Complete(1, t1); err != nil {
		t.Fatal(err)
	}
	if got, want := ids(l.All()), []int{1, 2, 3}; !slices.Equal(got, want) {
		t.Errorf("IDs after Complete = %v, want %v", got, want)
	}
}

func TestCompleteTwiceKeepsFirstCompletionTime(t *testing.T) {
	l := New()
	mustAdd(t, l, "a")
	if _, err := l.Complete(1, t0); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Complete(1, t1); !errors.Is(err, ErrAlreadyDone) {
		t.Errorf("second Complete error = %v, want %v", err, ErrAlreadyDone)
	}
	if got := l.All()[0].CompletedAt; !got.Equal(t0) {
		t.Errorf("CompletedAt = %v, want first completion %v", got, t0)
	}
}

func TestUnknownIDs(t *testing.T) {
	for _, id := range []int{0, -1, 3, math.MaxInt, math.MinInt} {
		l := New()
		mustAdd(t, l, "a", "b")
		if _, err := l.Complete(id, t1); !errors.Is(err, ErrNotFound) {
			t.Errorf("Complete(%d) error = %v, want %v", id, err, ErrNotFound)
		}
		if _, err := l.Delete(id); !errors.Is(err, ErrNotFound) {
			t.Errorf("Delete(%d) error = %v, want %v", id, err, ErrNotFound)
		}
		if len(l.All()) != 2 {
			t.Errorf("failed operation on ID %d changed the list", id)
		}
	}
}

func TestDeleteNeverRenumbersOrReusesIDs(t *testing.T) {
	l := New()
	mustAdd(t, l, "a", "b", "c")
	got, err := l.Delete(2)
	if err != nil {
		t.Fatalf("Delete(2) error = %v", err)
	}
	if got.Name != "b" {
		t.Errorf("Delete(2) returned %q, want %q", got.Name, "b")
	}
	if _, err := l.Delete(3); err != nil { // the newest ID: must still not be reused
		t.Fatal(err)
	}
	mustAdd(t, l, "d")
	if got, want := ids(l.All()), []int{1, 4}; !slices.Equal(got, want) {
		t.Errorf("IDs = %v, want %v", got, want)
	}
}

func TestReturnedSlicesDoNotAliasTheList(t *testing.T) {
	l := New()
	mustAdd(t, l, "a")
	l.All()[0].Name = "mutated"
	l.Pending()[0].Name = "mutated"
	if got := l.All()[0].Name; got != "a" {
		t.Errorf("Name = %q after mutating returned slices, want %q", got, "a")
	}
}

func TestCloneIsIndependent(t *testing.T) {
	l := New()
	mustAdd(t, l, "a")
	c := l.Clone()
	if _, err := c.Complete(1, t1); err != nil {
		t.Fatal(err)
	}
	mustAdd(t, c, "b")
	if l.All()[0].Done() || len(l.All()) != 1 || l.NextID() != 2 {
		t.Errorf("changing the clone changed the original: %+v, next ID %d", l.All(), l.NextID())
	}
}

func TestRestore(t *testing.T) {
	valid := func(id int) Task { return Task{ID: id, Name: "n", CreatedAt: t0} }
	tests := []struct {
		name    string
		nextID  int
		tasks   []Task
		wantIDs []int // nil means Restore must fail
	}{
		{"empty", 1, nil, []int{}},
		{"sorts by ID", 9, []Task{valid(5), valid(2), valid(8)}, []int{2, 5, 8}},
		{"next ID zero", 0, nil, nil},
		{"zero ID", 3, []Task{valid(0)}, nil},
		{"negative ID", 3, []Task{valid(-4)}, nil},
		{"ID equal to next ID", 3, []Task{valid(3)}, nil},
		{"duplicate ID", 3, []Task{valid(1), valid(1)}, nil},
		{"empty name", 3, []Task{{ID: 1, Name: " "}}, nil},
		{"control char in description", 3, []Task{{ID: 1, Name: "n", Description: "\x07"}}, nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			l, err := Restore(tc.nextID, tc.tasks)
			if tc.wantIDs == nil {
				if err == nil {
					t.Fatalf("Restore() = %v, want error", ids(l.All()))
				}
				return
			}
			if err != nil {
				t.Fatalf("Restore() error = %v", err)
			}
			if got := ids(l.All()); !slices.Equal(got, tc.wantIDs) {
				t.Errorf("IDs = %v, want %v", got, tc.wantIDs)
			}
			if l.NextID() != tc.nextID {
				t.Errorf("NextID() = %d, want %d", l.NextID(), tc.nextID)
			}
		})
	}
}

func TestRestoreDoesNotAliasInput(t *testing.T) {
	in := []Task{{ID: 2, Name: "b"}, {ID: 1, Name: "a"}}
	l, err := Restore(3, in)
	if err != nil {
		t.Fatal(err)
	}
	if in[0].ID != 2 {
		t.Error("Restore reordered the caller's slice")
	}
	in[1].Name = "mutated"
	if l.All()[0].Name != "a" {
		t.Error("Restore kept a reference to the caller's slice")
	}
}
