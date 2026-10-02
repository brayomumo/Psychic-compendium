package repl

import (
	"bytes"
	"context"
	"errors"
	"io"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/task"
)

var (
	t0      = time.Date(2026, 10, 2, 15, 47, 0, 0, time.UTC)
	nairobi = time.FixedZone("EAT", 3*60*60)
)

var errDiskFull = errors.New("disk full")

// fakeStore records every saved list. Its first failures saves fail.
type fakeStore struct {
	saves    []*task.List
	failures int
}

func (f *fakeStore) Save(l *task.List) error {
	if f.failures > 0 {
		f.failures--
		return errDiskFull
	}
	f.saves = append(f.saves, l.Clone())
	return nil
}

func (f *fakeStore) last() *task.List {
	if len(f.saves) == 0 {
		return task.New()
	}
	return f.saves[len(f.saves)-1]
}

type result struct {
	out, errOut string
	err         error
	store       *fakeStore
}

// runScript runs a scripted session to completion with a fixed clock that
// advances one minute per call, displaying times in UTC.
func runScript(t *testing.T, input string, list *task.List) result {
	t.Helper()
	return runWithStore(t, input, list, &fakeStore{})
}

func runWithStore(t *testing.T, input string, list *task.List, st *fakeStore) result {
	t.Helper()
	if list == nil {
		list = task.New()
	}
	var out, errOut bytes.Buffer
	now := t0
	err := Run(context.Background(), Config{
		In:       strings.NewReader(input),
		Out:      &out,
		Err:      &errOut,
		Store:    st,
		Now:      func() time.Time { now = now.Add(time.Minute); return now },
		Location: time.UTC,
	}, list)
	return result{out: out.String(), errOut: errOut.String(), err: err, store: st}
}

// withoutHelp strips the help text printed at startup.
func withoutHelp(out string) string { return strings.TrimPrefix(out, helpText) }

func TestFullSession(t *testing.T) {
	r := runScript(t, "e\nBuy milk\n2 litres\ne\nWrite README\n\nf\n1\nl\na\nq\n", nil)
	if r.err != nil {
		t.Fatalf("Run() error = %v", r.err)
	}
	want := "> Name: Description (optional): Added task 1: Buy milk\n" +
		"> Name: Description (optional): Added task 2: Write README\n" +
		"> Task ID to complete: Completed task 1: Buy milk\n" +
		"> ID  NAME          DESCRIPTION  CREATED\n" +
		"2   Write README               2026-10-02 15:49\n" +
		"> ID  STATUS   NAME          DESCRIPTION  CREATED           COMPLETED\n" +
		"1   done     Buy milk      2 litres     2026-10-02 15:48  2026-10-02 15:50\n" +
		"2   pending  Write README               2026-10-02 15:49\n" +
		"> "
	if got := withoutHelp(r.out); got != want {
		t.Errorf("output:\n%s\nwant:\n%s", got, want)
	}
	if r.errOut != "" {
		t.Errorf("unexpected diagnostics: %q", r.errOut)
	}
	if len(r.store.saves) != 3 {
		t.Errorf("saved %d times, want once per change (3)", len(r.store.saves))
	}
}

func TestHelpIsShownAtStartAndOnRequest(t *testing.T) {
	r := runScript(t, "h\n?\nq\n", nil)
	if got := strings.Count(r.out, helpText); got != 3 {
		t.Errorf("help shown %d times, want 3 (startup, h, ?)", got)
	}
}

// Regression: the first version ignored scanner.Scan()'s result, so at end
// of input it redrew the menu forever (20.6 million lines in 3 seconds).
func TestEOFInfiniteLoopIsFixed(t *testing.T) {
	for _, input := range []string{"", "l\n", "l", "e\nmilk\n\n"} {
		r := runScript(t, input, nil)
		if r.err != nil {
			t.Errorf("Run(%q) error = %v, want nil: end of input is a normal exit", input, r.err)
		}
		if n := len(r.out); n > 2*len(helpText)+200 {
			t.Errorf("Run(%q) wrote %d bytes, want bounded output", input, n)
		}
		if !strings.HasSuffix(r.out, "> \n") {
			t.Errorf("Run(%q) output ends %q, want the last prompt's line finished", input, r.out[max(0, len(r.out)-10):])
		}
	}
}

func TestEOFMidPromptAddsNothing(t *testing.T) {
	for _, input := range []string{"e\n", "e\nBuy milk\n", "f\n", "d\n"} {
		list := task.New()
		if _, err := list.Add("existing", "", t0); err != nil {
			t.Fatal(err)
		}
		r := runScript(t, input, list)
		if r.err != nil {
			t.Errorf("Run(%q) error = %v, want nil", input, r.err)
		}
		if len(r.store.saves) != 0 {
			t.Errorf("Run(%q) saved %d times, want none", input, len(r.store.saves))
		}
	}
}

// Regression: the layout "2006-02-01 12:59pm" swapped day and month and
// rendered 15:47:33 as "102:339pm".
func TestTimestampsUseYearMonthDayLayout(t *testing.T) {
	list, err := task.Restore(2, []task.Task{{
		ID: 1, Name: "x", CreatedAt: time.Date(2026, 10, 2, 15, 47, 33, 0, time.UTC),
	}})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		loc  *time.Location
		want string
	}{
		{time.UTC, "2026-10-02 15:47"},
		{nairobi, "2026-10-02 18:47"}, // stored in UTC, shown in local time
	} {
		var out bytes.Buffer
		err := Run(context.Background(), Config{
			In: strings.NewReader("a\n"), Out: &out, Err: io.Discard, Store: &fakeStore{}, Location: tc.loc,
		}, list.Clone())
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(out.String(), tc.want) {
			t.Errorf("in %v, listing:\n%s\nwant it to contain %q", tc.loc, out.String(), tc.want)
		}
	}
}

func TestCommandsAreCaseInsensitiveAndTrimmed(t *testing.T) {
	r := runScript(t, "E\nmilk\n\n  l  \nA\nQ\n", nil)
	if r.errOut != "" {
		t.Errorf("diagnostics = %q, want none", r.errOut)
	}
	if strings.Count(r.out, "milk") != 3 { // confirmation + two listings
		t.Errorf("output:\n%s\nwant the task added and listed twice", r.out)
	}
	if !strings.HasSuffix(r.out, "> ") {
		t.Errorf("Q did not quit cleanly; output ends %q", r.out[len(r.out)-10:])
	}
}

func TestRejectedInputKeepsSessionGoing(t *testing.T) {
	seed := func() *task.List {
		l := task.New()
		if _, err := l.Add("existing", "", t0); err != nil {
			t.Fatal(err)
		}
		return l
	}
	tests := []struct {
		name, input, wantErr string
	}{
		{"unknown command", "x\n", `unknown command "x"; type h for help`},
		{"escape sequence echoed safely", "\x1b[2J\n", `unknown command "\x1b[2J"`},
		{"empty name", "e\n\n", "error: task name is empty"},
		{"whitespace name", "e\n   \n", "error: task name is empty"},
		{"name with tab", "e\na\tb\n", "control characters"},
		{"description too long", "e\nok\n" + strings.Repeat("x", task.MaxDescriptionLen+1) + "\n", "limit is 1000"},
		{"not a number", "f\nx\n", `error: "x" is not a task ID`},
		{"empty ID", "f\n\n", `error: "" is not a task ID`},
		{"number too large for int", "f\n99999999999999999999\n", `"99999999999999999999" is not a task ID`},
		{"decimal", "d\n1.5\n", `"1.5" is not a task ID`},
		{"out of range", "f\n9\n", "error: task not found: 9"},
		{"zero", "d\n0\n", "error: task not found: 0"},
		{"negative", "f\n-1\n", "error: task not found: -1"},
		// A second pending task keeps f prompting after task 1 is done.
		{"already done", "e\nother\n\nf\n1\nf\n1\n", "error: task already completed: 1"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := runScript(t, tc.input+"a\n", seed())
			if r.err != nil {
				t.Fatalf("Run() error = %v", r.err)
			}
			if !strings.Contains(r.errOut, tc.wantErr) {
				t.Errorf("diagnostics = %q, want %q", r.errOut, tc.wantErr)
			}
			// The session continued: the trailing "a" listed the seeded task.
			if !strings.Contains(r.out, "existing") {
				t.Errorf("session did not continue after rejected input; output:\n%s", r.out)
			}
		})
	}
}

func TestEmptyNameDoesNotAskForDescription(t *testing.T) {
	r := runScript(t, "e\n\n", nil)
	if strings.Contains(r.out, "Description") {
		t.Errorf("asked for a description after an empty name:\n%s", r.out)
	}
}

func TestCompleteAndDeleteOnEmptyListDoNotPrompt(t *testing.T) {
	r := runScript(t, "f\nd\n", nil)
	want := "> No pending tasks.\n> No tasks.\n> \n"
	if got := withoutHelp(r.out); got != want {
		t.Errorf("output = %q, want %q", got, want)
	}
}

func TestCompleteWhenEverythingIsDoneDoesNotPrompt(t *testing.T) {
	r := runScript(t, "e\na\n\nf\n1\nf\n", nil)
	if !strings.HasSuffix(r.out, "> No pending tasks.\n> \n") {
		t.Errorf("output:\n%s\nwant f to report no pending tasks without prompting", r.out)
	}
}

func TestDeleteKeepsOtherIDsStable(t *testing.T) {
	r := runScript(t, "e\na\n\ne\nb\n\ne\nc\n\nd\n2\ne\nd\n\nl\n", nil)
	got := r.store.last().All()
	ids := make([]int, len(got))
	for i, tk := range got {
		ids[i] = tk.ID
	}
	if want := []int{1, 3, 4}; !slices.Equal(ids, want) {
		t.Errorf("IDs after deleting 2 and adding one = %v, want %v", ids, want)
	}
	if !strings.Contains(r.out, "Deleted task 2: b") {
		t.Errorf("no delete confirmation in:\n%s", r.out)
	}
}

func TestListingsDoNotSave(t *testing.T) {
	list := task.New()
	if _, err := list.Add("x", "", t0); err != nil {
		t.Fatal(err)
	}
	if r := runScript(t, "l\na\nh\nx\n\n", list); len(r.store.saves) != 0 {
		t.Errorf("read-only commands saved %d times", len(r.store.saves))
	}
}

func TestFailedSaveDiscardsChange(t *testing.T) {
	// The first add fails to save, so nothing is pending, and the second add
	// must get ID 1: the failed change left no trace in memory.
	r := runWithStore(t, "e\nmilk\n\nl\ne\nbread\n\nq\n", nil, &fakeStore{failures: 1})
	if r.err != nil {
		t.Fatalf("Run() error = %v", r.err)
	}
	if want := "error: change not saved, nothing was modified: disk full\n"; r.errOut != want {
		t.Errorf("diagnostics = %q, want %q", r.errOut, want)
	}
	if !strings.Contains(r.out, "No pending tasks.") || !strings.Contains(r.out, "Added task 1: bread") {
		t.Errorf("output:\n%s\nwant the failed add discarded and the next add to get ID 1", r.out)
	}
	if strings.Contains(r.out, "Added task 1: milk") {
		t.Errorf("confirmed a change that was not saved:\n%s", r.out)
	}
}

// promptWriter signals each time a prompt has been written, so tests know the
// session is blocked waiting for input without sleeping.
type promptWriter struct {
	bytes.Buffer
	prompted chan struct{}
}

func (w *promptWriter) Write(p []byte) (int, error) {
	n, err := w.Buffer.Write(p)
	if bytes.HasSuffix(p, []byte(": ")) || bytes.HasSuffix(p, []byte("> ")) {
		w.prompted <- struct{}{}
	}
	return n, err
}

func TestCancelWhileWaitingForInput(t *testing.T) {
	for _, tc := range []struct{ name, before string }{
		{"at the command prompt", ""},
		{"mid add, at the name prompt", "e\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			errInterrupted := errors.New("interrupted")
			ctx, cancel := context.WithCancelCause(context.Background())
			in, inW := io.Pipe()
			defer inW.Close() // releases the reader goroutine
			out := &promptWriter{prompted: make(chan struct{}, 10)}
			st := &fakeStore{}
			done := make(chan error, 1)
			go func() { done <- Run(ctx, Config{In: in, Out: out, Err: io.Discard, Store: st}, task.New()) }()

			<-out.prompted
			if tc.before != "" {
				if _, err := io.WriteString(inW, tc.before); err != nil {
					t.Fatal(err)
				}
				<-out.prompted
			}
			cancel(errInterrupted)

			select {
			case err := <-done:
				if !errors.Is(err, errInterrupted) {
					t.Errorf("Run() error = %v, want the cancellation cause", err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("Run did not return after cancellation")
			}
			if len(st.saves) != 0 {
				t.Errorf("saved %d times after cancellation, want 0", len(st.saves))
			}
		})
	}
}

func TestCancellationWinsOverWaitingInput(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	st := &fakeStore{}
	err := Run(ctx, Config{In: strings.NewReader("e\nmilk\n\n"), Out: io.Discard, Err: io.Discard, Store: st}, task.New())
	if !errors.Is(err, context.Canceled) {
		t.Errorf("Run() error = %v, want %v", err, context.Canceled)
	}
	if len(st.saves) != 0 {
		t.Errorf("ran a command after cancellation: %d saves", len(st.saves))
	}
}

// failingWriter accepts n writes and then fails, like a closed pipe.
type failingWriter struct{ n int }

func (w *failingWriter) Write(p []byte) (int, error) {
	if w.n == 0 {
		return 0, errors.New("broken pipe")
	}
	w.n--
	return len(p), nil
}

// Same class of bug as the EOF loop: a session whose output is gone must
// stop, not keep reading commands it can never answer.
func TestWriteFailureEndsSession(t *testing.T) {
	for _, n := range []int{0, 1, 3} {
		input := strings.Repeat("l\n", 1000)
		err := Run(context.Background(), Config{
			In: strings.NewReader(input), Out: &failingWriter{n: n}, Err: io.Discard, Store: &fakeStore{},
		}, task.New())
		if err == nil || !strings.Contains(err.Error(), "broken pipe") {
			t.Errorf("after %d writes: Run() error = %v, want the write error", n, err)
		}
	}
}

func TestOverlongLineIsAnError(t *testing.T) {
	input := "e\n" + strings.Repeat("x", 70*1024) + "\n"
	r := runScript(t, input, nil)
	if r.err == nil || !strings.Contains(r.err.Error(), "line longer than") {
		t.Errorf("Run() error = %v, want a line-too-long error", r.err)
	}
	if len(r.store.saves) != 0 {
		t.Errorf("saved a task from an overlong line")
	}
}

type errReader struct{}

func (errReader) Read([]byte) (int, error) { return 0, errors.New("input device gone") }

func TestReadFailureIsAnError(t *testing.T) {
	err := Run(context.Background(), Config{In: errReader{}, Out: io.Discard, Err: io.Discard, Store: &fakeStore{}}, task.New())
	if err == nil || !strings.Contains(err.Error(), "read input: input device gone") {
		t.Errorf("Run() error = %v, want the read error", err)
	}
}

func TestConfigValidateReportsEveryMissingField(t *testing.T) {
	err := Config{}.Validate()
	for _, field := range []string{"In", "Out", "Err", "Store"} {
		if err == nil || !strings.Contains(err.Error(), "missing "+field) {
			t.Errorf("Validate() = %v, want it to report %s", err, field)
		}
	}
	ok := Config{In: strings.NewReader(""), Out: io.Discard, Err: io.Discard, Store: &fakeStore{}}
	if err := ok.Validate(); err != nil {
		t.Errorf("Validate() on a complete config = %v, want nil", err)
	}
}

func TestRunRejectsInvalidInputsBeforeDoingAnything(t *testing.T) {
	if err := Run(context.Background(), Config{Out: io.Discard}, task.New()); err == nil ||
		!strings.HasPrefix(err.Error(), "invalid config: ") {
		t.Errorf("Run(incomplete config) = %v, want an invalid config error", err)
	}
	var out bytes.Buffer
	cfg := Config{In: strings.NewReader("q\n"), Out: &out, Err: io.Discard, Store: &fakeStore{}}
	if err := Run(context.Background(), cfg, nil); err == nil {
		t.Error("Run(nil list) = nil, want an error")
	}
	if out.Len() != 0 {
		t.Errorf("Run wrote %q before rejecting its inputs", out.String())
	}
}
