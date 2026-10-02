// Command go-concurrency runs a producer/consumer pipeline built on goroutines and one shared
// channel, then prints what happened: how the jobs were spread across consumers and how long
// producers spent blocked by backpressure.
//
// Usage:
//
//	go-concurrency [-producers N] [-consumers N] [-jobs N] [-buffer N] [-work D] [-seed N] [-v]
//
// SIGINT (Ctrl+C) or SIGTERM cancels the run: producers stop, consumers stop, the partial summary is
// printed and the exit status is 130.
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
	"syscall"
	"time"

	"github.com/brayomumo/Psychic-compendium/go-concurrency/internal/pipeline"
)

// Exit statuses.
const (
	exitOK          = 0
	exitError       = 1
	exitUsage       = 2   // bad flags or arguments, as the flag package does by default
	exitInterrupted = 130 // shell convention for "terminated by Ctrl+C" (128 + SIGINT)
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

// run is main without the process-global parts (signals, os.Args, os.Exit), so tests can drive it.
// The summary goes to stdout; usage, per-job logs and errors go to stderr.
func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("go-concurrency", flag.ContinueOnError)
	fs.SetOutput(stderr)
	var cfg pipeline.Config
	fs.IntVar(&cfg.Producers, "producers", 2, "producer goroutines, 1 to 10000")
	fs.IntVar(&cfg.Consumers, "consumers", 4, "consumer goroutines, 1 to 10000")
	fs.IntVar(&cfg.Jobs, "jobs", 100, "total jobs, split across producers")
	fs.IntVar(&cfg.Buffer, "buffer", 10, "channel capacity; 0 = unbuffered")
	fs.DurationVar(&cfg.Work, "work", 10*time.Millisecond, "mean simulated work per job, e.g. 0, 5ms, 1s")
	fs.Uint64Var(&cfg.Seed, "seed", 1, "seed for the per-job work durations")
	verbose := fs.Bool("v", false, "log every finished job to stderr")

	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitOK
		}
		return exitUsage // the flag package has already printed the error and usage
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "unexpected arguments: %q\n", fs.Args())
		fs.Usage()
		return exitUsage
	}
	if err := cfg.Validate(); err != nil {
		fmt.Fprintf(stderr, "invalid flags:\n%v\n", err)
		fs.Usage()
		return exitUsage
	}
	if *verbose {
		logger := log.New(stderr, "", log.Lmicroseconds) // a log.Logger is safe for concurrent use
		cfg.OnJob = func(consumer int, job pipeline.Job) {
			logger.Printf("consumer %d finished job %d from producer %d (work %v)",
				consumer, job.ID, job.Producer, job.Work.Round(time.Microsecond))
		}
	}

	fmt.Fprintf(stdout, "running: producers=%d consumers=%d jobs=%d buffer=%d work=%v seed=%d\n",
		cfg.Producers, cfg.Consumers, cfg.Jobs, cfg.Buffer, cfg.Work, cfg.Seed)
	stats, err := pipeline.Run(ctx, cfg)
	printSummary(stdout, cfg, stats)

	switch {
	case err == nil:
		return exitOK
	case errors.Is(err, context.Canceled):
		fmt.Fprintf(stderr, "interrupted: finished %d of %d jobs; %d produced jobs were left unfinished\n",
			stats.Consumed, cfg.Jobs, stats.Produced-stats.Consumed)
		return exitInterrupted
	default:
		fmt.Fprintf(stderr, "error: %v\n", err)
		return exitError
	}
}

func printSummary(w io.Writer, cfg pipeline.Config, s pipeline.Stats) {
	fmt.Fprintf(w, "consumed %d/%d jobs in %v (produced %d)\n",
		s.Consumed, cfg.Jobs, roundDuration(s.Elapsed), s.Produced)
	fmt.Fprintf(w, "per consumer: %v\n", s.PerConsumer)
	fmt.Fprintf(w, "producers blocked on send: %v in total (backpressure)\n", roundDuration(s.SendWait))
}

// roundDuration keeps three or so significant digits for both microsecond and multi-second runs.
func roundDuration(d time.Duration) time.Duration {
	if d < time.Millisecond {
		return d.Round(time.Microsecond)
	}
	return d.Round(time.Millisecond)
}
