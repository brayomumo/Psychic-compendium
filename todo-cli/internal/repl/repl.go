// Package repl implements the interactive todo prompt. It reads commands from
// an io.Reader and writes to io.Writers, so the whole user interface can be
// driven by tests without a terminal.
package repl

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/task"
)

// TimeLayout is the single layout used to display timestamps. Go layouts are
// spelled with the reference time Mon Jan 2 15:04:05 MST 2006, so "01" is the
// month, "02" the day and "15:04" a 24-hour clock.
const TimeLayout = "2006-01-02 15:04"

const helpText = `Commands:
  e  add a task
  f  finish (complete) a task
  l  list pending tasks
  a  list all tasks
  d  delete a task
  h  show this help
  q  quit (end of input, Ctrl+D, also quits)
`

// Store persists the task list. It is declared here, by its only consumer, so
// tests can substitute an in-memory fake; *store.File satisfies it.
type Store interface {
	Save(l *task.List) error
}

// Config wires a session to its environment.
type Config struct {
	In  io.Reader // commands and answers, one per line
	Out io.Writer // prompts, confirmations and listings
	Err io.Writer // messages about rejected input and failed changes
	// Store persists every change; a change is only applied once saved.
	Store Store
	// Now returns the current time. Nil means time.Now.
	Now func() time.Time
	// Location is the zone timestamps are displayed in. Nil means time.Local.
	Location *time.Location
}

// Validate reports every missing required field at once.
func (c Config) Validate() error {
	var errs []error
	if c.In == nil {
		errs = append(errs, errors.New("missing In"))
	}
	if c.Out == nil {
		errs = append(errs, errors.New("missing Out"))
	}
	if c.Err == nil {
		errs = append(errs, errors.New("missing Err"))
	}
	if c.Store == nil {
		errs = append(errs, errors.New("missing Store"))
	}
	return errors.Join(errs...)
}

// errQuit is how the q command unwinds the loop; it never leaves Run.
var errQuit = errors.New("quit")

// Run drives an interactive session over list until the user quits, input
// ends, or ctx is cancelled. Run takes ownership of list.
//
// Every change is applied to a copy, saved, and only then adopted: if saving
// fails the change is discarded and the session carries on, so what the user
// sees never runs ahead of what is on disk.
//
// Run returns nil after q or end of input, context.Cause(ctx) after
// cancellation, and an error if reading input or writing output fails.
// Cancellation is noticed while waiting for input; a change that is being
// saved always completes first. An invalid cfg or a nil list is reported
// before anything is read or written.
func Run(ctx context.Context, cfg Config, list *task.List) error {
	if err := cfg.Validate(); err != nil {
		return fmt.Errorf("invalid config: %w", err)
	}
	if list == nil {
		return errors.New("nil task list")
	}
	done := make(chan struct{})
	defer close(done)
	s := &session{
		ctx:   ctx,
		lines: readLines(cfg.In, done),
		out:   &printer{w: cfg.Out},
		diag:  &printer{w: cfg.Err},
		store: cfg.Store,
		now:   cfg.Now,
		loc:   cfg.Location,
		list:  list,
	}
	if s.now == nil {
		s.now = time.Now
	}
	if s.loc == nil {
		s.loc = time.Local
	}
	return s.loop()
}

type session struct {
	ctx   context.Context
	lines <-chan line
	out   *printer
	// diag write errors are deliberately not checked: if the error stream
	// is gone there is nowhere left to report anything.
	diag  *printer
	store Store
	now   func() time.Time
	loc   *time.Location
	list  *task.List
}

func (s *session) loop() error {
	s.out.printf("%s", helpText)
	for {
		err := s.step()
		switch {
		case err == nil:
			continue
		case errors.Is(err, errQuit):
			return s.out.err
		case errors.Is(err, io.EOF):
			// End the prompt's line so the shell prompt starts on a new one.
			s.out.printf("\n")
			return s.out.err
		default:
			if s.ctx.Err() != nil {
				s.out.printf("\n")
			}
			return err
		}
	}
}

// step reads and runs one command. It returns an error only to end the
// session; rejected input is reported to the user and is not an error here.
func (s *session) step() error {
	input, err := s.readLine("> ")
	if err != nil {
		return err
	}
	if err := s.dispatch(input); err != nil {
		return err
	}
	// The first version ignored every I/O failure; stop as soon as output
	// can no longer be written rather than prompting into the void.
	return s.out.err
}

func (s *session) dispatch(input string) error {
	input = strings.TrimSpace(input)
	switch strings.ToLower(input) {
	case "":
		return nil
	case "e":
		return s.add()
	case "f":
		if len(s.list.Pending()) == 0 {
			s.out.printf("No pending tasks.\n")
			return nil
		}
		return s.changeByID("Task ID to complete: ", func(l *task.List, id int) (string, error) {
			t, err := l.Complete(id, s.now())
			return fmt.Sprintf("Completed task %d: %s", t.ID, t.Name), err
		})
	case "d":
		if len(s.list.All()) == 0 {
			s.out.printf("No tasks.\n")
			return nil
		}
		return s.changeByID("Task ID to delete: ", func(l *task.List, id int) (string, error) {
			t, err := l.Delete(id)
			return fmt.Sprintf("Deleted task %d: %s", t.ID, t.Name), err
		})
	case "l":
		s.printTasks(s.list.Pending(), false)
	case "a":
		s.printTasks(s.list.All(), true)
	case "h", "?":
		s.out.printf("%s", helpText)
	case "q":
		return errQuit
	default:
		// %q escapes control characters, so echoing input is safe.
		s.diag.printf("unknown command %q; type h for help\n", input)
	}
	return nil
}

func (s *session) add() error {
	name, err := s.readLine("Name: ")
	if err != nil {
		return err
	}
	// Reject a bad name now instead of after asking for a description.
	if err := task.ValidateName(name); err != nil {
		s.diag.printf("error: %v\n", err)
		return nil
	}
	description, err := s.readLine("Description (optional): ")
	if err != nil {
		return err
	}
	s.apply(func(l *task.List) (string, error) {
		t, err := l.Add(name, description, s.now())
		return fmt.Sprintf("Added task %d: %s", t.ID, t.Name), err
	})
	return nil
}

// changeByID prompts for a task ID and applies change to it.
func (s *session) changeByID(prompt string, change func(l *task.List, id int) (string, error)) error {
	input, err := s.readLine(prompt)
	if err != nil {
		return err
	}
	input = strings.TrimSpace(input)
	id, err := strconv.Atoi(input)
	if err != nil {
		s.diag.printf("error: %q is not a task ID; IDs are whole numbers, as shown by l or a\n", input)
		return nil
	}
	s.apply(func(l *task.List) (string, error) { return change(l, id) })
	return nil
}

// apply runs change on a copy of the list, saves the copy, and only then
// adopts it. change returns the confirmation to print on success.
func (s *session) apply(change func(l *task.List) (string, error)) {
	next := s.list.Clone()
	msg, err := change(next)
	if err != nil {
		s.diag.printf("error: %v\n", err)
		return
	}
	if err := s.store.Save(next); err != nil {
		s.diag.printf("error: change not saved, nothing was modified: %v\n", err)
		return
	}
	s.list = next
	s.out.printf("%s\n", msg)
}

func (s *session) printTasks(tasks []task.Task, all bool) {
	if len(tasks) == 0 {
		if all {
			s.out.printf("No tasks.\n")
		} else {
			s.out.printf("No pending tasks.\n")
		}
		return
	}
	tw := tabwriter.NewWriter(s.out, 0, 0, 2, ' ', 0)
	if all {
		writeRow(tw, "ID", "STATUS", "NAME", "DESCRIPTION", "CREATED", "COMPLETED")
	} else {
		writeRow(tw, "ID", "NAME", "DESCRIPTION", "CREATED")
	}
	for _, t := range tasks {
		id, created := strconv.Itoa(t.ID), s.format(t.CreatedAt)
		if !all {
			writeRow(tw, id, t.Name, t.Description, created)
			continue
		}
		status, completed := "pending", ""
		if t.Done() {
			status, completed = "done", s.format(t.CompletedAt)
		}
		writeRow(tw, id, status, t.Name, t.Description, created, completed)
	}
	// A write error is recorded by s.out and ends the session in step.
	_ = tw.Flush()
}

func (s *session) format(t time.Time) string { return t.In(s.loc).Format(TimeLayout) }

// writeRow writes tab-separated cells, dropping trailing empty cells so rows
// carry no trailing padding.
func writeRow(w io.Writer, cells ...string) {
	for len(cells) > 0 && cells[len(cells)-1] == "" {
		cells = cells[:len(cells)-1]
	}
	_, _ = io.WriteString(w, strings.Join(cells, "\t")+"\n")
}

// readLine prints prompt and waits for the next line of input or for
// cancellation, whichever comes first.
func (s *session) readLine(prompt string) (string, error) {
	// Cancellation wins over input that is already waiting, so Ctrl+C is
	// never followed by one more command running.
	if err := context.Cause(s.ctx); err != nil {
		return "", err
	}
	s.out.printf("%s", prompt)
	if s.out.err != nil {
		return "", s.out.err
	}
	select {
	case <-s.ctx.Done():
		return "", context.Cause(s.ctx)
	case l, ok := <-s.lines:
		switch {
		case !ok:
			return "", io.EOF
		case errors.Is(l.err, bufio.ErrTooLong):
			return "", fmt.Errorf("read input: line longer than %d bytes: %w", bufio.MaxScanTokenSize, l.err)
		case l.err != nil && !errors.Is(l.err, io.EOF):
			return "", fmt.Errorf("read input: %w", l.err)
		}
		return l.text, l.err
	}
}

type line struct {
	text string
	err  error // io.EOF at end of input
}

// readLines delivers lines from r until it ends or done is closed. Reading
// happens on its own goroutine because a blocking Read on a terminal cannot
// be interrupted; this way the session can still react to cancellation.
// The goroutine exits as soon as done is closed or input ends. If it is
// blocked inside Read at that moment it exits after that Read returns,
// which for a terminal is when the process exits.
func readLines(r io.Reader, done <-chan struct{}) <-chan line {
	ch := make(chan line)
	go func() {
		defer close(ch)
		sc := bufio.NewScanner(r)
		for sc.Scan() {
			select {
			case ch <- line{text: sc.Text()}:
			case <-done:
				return
			}
		}
		err := sc.Err()
		if err == nil {
			err = io.EOF
		}
		select {
		case ch <- line{err: err}:
		case <-done:
		}
	}()
	return ch
}

// printer writes formatted output and remembers the first write error, so
// call sites stay readable and the loop checks for failure in one place.
type printer struct {
	w   io.Writer
	err error
}

func (p *printer) printf(format string, args ...any) {
	if p.err != nil {
		return
	}
	if _, err := fmt.Fprintf(p.w, format, args...); err != nil {
		p.err = fmt.Errorf("write output: %w", err)
	}
}

// Write lets a printer sit under a tabwriter.
func (p *printer) Write(b []byte) (int, error) {
	if p.err != nil {
		return 0, p.err
	}
	n, err := p.w.Write(b)
	if err != nil {
		p.err = fmt.Errorf("write output: %w", err)
	}
	return n, p.err
}
