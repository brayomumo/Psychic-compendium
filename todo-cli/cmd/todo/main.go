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
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// run is main without the process-global parts, so tests can call it.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
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
		logger.Printf("unexpected argument %q; run todo -h for usage", flags.Arg(0))
		return exitUsage
	}
	if *path == "" {
		if defaultErr != nil {
			logger.Printf("no default location for the data file (%v); pass -file", defaultErr)
		} else {
			logger.Print("-file must not be empty")
		}
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

	ctx, stop := notifyContext()
	defer stop()
	shown := *path
	if abs, err := filepath.Abs(shown); err == nil {
		shown = abs
	}
	logger.Printf("tasks are saved to %s", shown)
	err = repl.Run(ctx, repl.Config{In: stdin, Out: stdout, Err: stderr, Store: st}, list)

	var sig signalError
	switch {
	case err == nil:
		return exitOK
	case errors.As(err, &sig):
		// Every confirmed change was saved before it was confirmed, and a
		// save in progress is never interrupted, so nothing is lost here.
		logger.Printf("stopped by %v; all confirmed changes are saved", sig.sig)
		return 128 + int(sig.sig)
	default:
		logger.Print(err)
		return exitFailure
	}
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
// After the first signal, handling is reset to the default, so a second
// Ctrl+C terminates immediately if shutdown ever hangs.
func notifyContext() (context.Context, func()) {
	ctx, cancel := context.WithCancelCause(context.Background())
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		select {
		case s := <-signals:
			signal.Stop(signals)
			sig, _ := s.(syscall.Signal) // always a syscall.Signal on Unix
			cancel(signalError{sig: sig})
		case <-ctx.Done():
		}
	}()
	return ctx, func() {
		signal.Stop(signals)
		cancel(nil)
	}
}
