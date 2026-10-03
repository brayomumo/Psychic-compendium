// Command todo is an interactive todo list that saves every change to a JSON
// file before confirming it.
//
// Usage:
//
//	todo [-file path]
//
// Exit status: 0 after q or end of input, 1 on a runtime failure (such as an
// unreadable or corrupt data file), 2 on a usage error, and 128+n after a
// clean shutdown on signal n (130 for SIGINT, 143 for SIGTERM).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/repl"
	"github.com/brayomumo/Psychic-compendium/todo-cli/internal/store"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

func main() {
	// With SIGPIPE at its default, the Go runtime kills the process when a
	// write to stdout hits a closed pipe (todo | head -1). Ignoring it turns
	// that into an EPIPE write error, which ends the session with exit 1 like
	// any other local I/O failure.
	signal.Ignore(syscall.SIGPIPE)
	logger := log.New(os.Stderr, "todo: ", 0)
	ctx, stop := notifyContext(logger)
	code := run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr)
	// stop runs before os.Exit, which skips deferred calls. A signal that
	// raced the end of the session (Ctrl+C then Ctrl+D) was received but not
	// yet seen by run; it still decides the exit status.
	if sig := stop(); sig != 0 && code < 128 {
		logger.Printf("stopped by %v; all confirmed changes are saved", sig)
		code = 128 + int(sig)
	}
	os.Exit(code)
}

// run is main without the process-global parts, so tests can call it. ctx is
// cancelled when the session must stop; a signalError cause selects the
// 128+n exit status.
func run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	logger := log.New(stderr, "todo: ", 0)

	defaultPath, defaultErr := defaultFile()
	flags := flag.NewFlagSet("todo", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.Usage = func() {
		fmt.Fprintf(flags.Output(), "Usage: todo [-file path]\n\nAn interactive todo list. Type h at the prompt for commands.\n\n")
		flags.PrintDefaults()
	}
	path := flags.String("file", defaultPath, "`path` of the JSON file tasks are saved in")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitUsage // flag already printed the problem and usage
	}
	if flags.NArg() > 0 {
		logger.Printf("unexpected argument %q", flags.Arg(0))
		flags.Usage()
		return exitUsage
	}
	if *path == "" {
		if defaultErr != nil {
			logger.Printf("no default location for the data file (%v); pass -file", defaultErr)
		} else {
			logger.Print("-file must not be empty")
		}
		flags.Usage()
		return exitUsage
	}

	st := store.NewFile(*path)
	list, err := st.Load()
	if err != nil {
		logger.Print(err)
		if errors.Is(err, store.ErrCorrupt) {
			logger.Print("the file was not modified; repair it, or move it aside to start a new list")
		}
		return exitFailure
	}

	shown := *path
	if abs, err := filepath.Abs(shown); err == nil {
		shown = abs
	}
	logger.Printf("tasks are saved to %s", shown)
	err = repl.Run(ctx, repl.Config{In: stdin, Out: stdout, Err: stderr, Store: st}, list)
	return exitStatus(ctx, err, logger)
}

// exitStatus maps how the session ended to the process exit status.
//
// A received signal wins over every other outcome. When end of input races
// the signal, the session ends with a nil error, and exiting 0 would hide that
// the process was told to stop.
func exitStatus(ctx context.Context, err error, logger *log.Logger) int {
	var sig signalError
	if errors.As(context.Cause(ctx), &sig) || errors.As(err, &sig) {
		// Every confirmed change was saved before it was confirmed, and a
		// save in progress is never interrupted, so nothing is lost here.
		logger.Printf("stopped by %v; all confirmed changes are saved", sig.sig)
		return 128 + int(sig.sig)
	}
	if err != nil {
		// Includes a cancellation no signal caused: that is a failure, never
		// a clean shutdown (STANDARDS.md section 10).
		logger.Print(err)
		return exitFailure
	}
	return exitOK
}

// defaultFile is where tasks live unless -file says otherwise: the per-user
// config directory (~/Library/Application Support on macOS, $XDG_CONFIG_HOME
// or ~/.config on Linux), so the same list is found from any directory.
func defaultFile() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "todo-cli", "tasks.json"), nil
}

// signalError is the cancellation cause recorded when a signal arrives.
type signalError struct{ sig syscall.Signal }

func (e signalError) Error() string { return "received " + e.sig.String() }

// notifyContext returns a context cancelled by SIGINT or SIGTERM, with the
// signal as the cancellation cause. signal.NotifyContext would not say which
// signal arrived, and the exit status follows the 128+n convention.
//
// The first signal resets handling to the default, records the signal as the
// cause, and then logs a notice, in that order: by the time anyone reads the
// notice, a second Ctrl+C already terminates the process, and the exit status
// is already decided.
//
// stop releases the signals and returns the signal received, or 0. It waits
// for the watcher, so a signal the watcher has taken is never lost, and it
// collects one that arrived but was not yet taken.
func notifyContext(logger *log.Logger) (context.Context, func() syscall.Signal) {
	ctx, cancel := context.WithCancelCause(context.Background())
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	var received syscall.Signal
	record := func(s os.Signal) {
		received, _ = s.(syscall.Signal) // always a syscall.Signal on Unix
		cancel(signalError{sig: received})
		logger.Printf("received %v, finishing up (send it again to quit at once)", received)
	}
	done := make(chan struct{})
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		select {
		case s := <-signals:
			signal.Stop(signals)
			record(s)
		case <-done:
		}
	}()
	return ctx, func() syscall.Signal {
		signal.Stop(signals) // no further signals reach the channel
		close(done)
		<-finished // the watcher has recorded whatever it took
		if received == 0 {
			select {
			case s := <-signals: // arrived before Stop, not yet taken
				record(s)
			default:
			}
		}
		cancel(nil)
		return received
	}
}
