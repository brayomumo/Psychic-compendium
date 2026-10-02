// Package pipeline runs a multi-producer, multi-consumer work pipeline over one shared channel.
//
// It exists to demonstrate four rules:
//
//   - A blocking send is the backpressure. Producers never poll; when the buffer is full they block
//     until a consumer receives or the context is cancelled.
//   - Exactly one party closes the channel, and only after every sender has returned. With several
//     producers that party is Run, which waits for all producers before closing.
//   - Consumers range over the channel, so they stop on their own once it is closed and drained.
//   - Cancellation flows through a context. Every blocking operation also watches ctx.Done(), so no
//     goroutine is left blocked when Run returns.
package pipeline

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"sync"
	"time"
)

// Guardrails for Config. They keep a mistyped flag from exhausting memory or overflowing the
// job-ID arithmetic; they are far above anything a demo needs.
const (
	MaxWorkers = 10_000        // upper bound for Producers and for Consumers
	MaxJobs    = 1_000_000_000 // upper bound for Jobs
	MaxBuffer  = 1_000_000     // upper bound for Buffer
	MaxWork    = time.Hour     // upper bound for Work
)

// Job is one unit of work.
type Job struct {
	ID       int           // unique across all producers, in [0, Config.Jobs)
	Producer int           // index of the producer that created the job
	Work     time.Duration // simulated processing time
}

// Config describes one pipeline run. The zero value is not valid; see Validate.
type Config struct {
	Producers int           // producer goroutines, in [1, MaxWorkers]
	Consumers int           // consumer goroutines, in [1, MaxWorkers]
	Jobs      int           // total jobs, split evenly across producers; 0 is allowed
	Buffer    int           // channel capacity; 0 makes every send a rendezvous with a receiver
	Work      time.Duration // mean simulated time per job; each job draws uniformly from [0, 2*Work)
	Seed      uint64        // seeds the per-producer random sources, so job durations are reproducible

	// OnJob, if non-nil, is called by a consumer each time it finishes a job. Consumers call it
	// concurrently, so it must be safe for concurrent use. It runs on the consumer's goroutine,
	// so a slow OnJob slows that consumer down.
	OnJob func(consumer int, job Job)
}

// Validate reports every out-of-range field in c, joined into one error.
func (c Config) Validate() error {
	var errs []error
	check := func(ok bool, format string, args ...any) {
		if !ok {
			errs = append(errs, fmt.Errorf(format, args...))
		}
	}
	check(c.Producers >= 1 && c.Producers <= MaxWorkers,
		"producers must be in [1, %d], got %d", MaxWorkers, c.Producers)
	check(c.Consumers >= 1 && c.Consumers <= MaxWorkers,
		"consumers must be in [1, %d], got %d", MaxWorkers, c.Consumers)
	check(c.Jobs >= 0 && c.Jobs <= MaxJobs,
		"jobs must be in [0, %d], got %d", MaxJobs, c.Jobs)
	check(c.Buffer >= 0 && c.Buffer <= MaxBuffer,
		"buffer must be in [0, %d], got %d", MaxBuffer, c.Buffer)
	check(c.Work >= 0 && c.Work <= MaxWork,
		"work must be in [0, %v], got %v", MaxWork, c.Work)
	return errors.Join(errs...)
}

// Stats describes what a run did. The counts are exact, including after cancellation.
type Stats struct {
	Produced    int           // jobs handed to the channel
	Consumed    int           // jobs whose processing finished
	PerConsumer []int         // Consumed, indexed by consumer
	SendWait    time.Duration // time producers spent blocked on send, summed: the cost of backpressure
	Elapsed     time.Duration // wall-clock duration of Run
}

// Run starts cfg.Producers producers and cfg.Consumers consumers, waits for every one of them to
// return, and reports what happened.
//
// It returns a nil error once every job has been consumed. If ctx is cancelled first, producers stop
// sending, consumers stop taking jobs (any still buffered are abandoned, and a job interrupted
// mid-work does not count as consumed), and Run returns ctx.Err() with the partial Stats. In both
// cases no goroutine started by Run is still running when it returns.
func Run(ctx context.Context, cfg Config) (Stats, error) {
	if err := cfg.Validate(); err != nil {
		return Stats{}, fmt.Errorf("invalid config: %w", err)
	}
	start := time.Now()
	jobs := make(chan Job, cfg.Buffer)

	// Each goroutine writes only its own slice element, and the elements are read only after
	// the matching WaitGroup.Wait, which orders those writes before the reads. No mutex needed.
	produced := make([]int, cfg.Producers)
	sendWait := make([]time.Duration, cfg.Producers)
	var producers sync.WaitGroup
	for p := range cfg.Producers {
		producers.Add(1)
		go func() {
			defer producers.Done()
			produced[p], sendWait[p] = produce(ctx, cfg, p, jobs)
		}()
	}

	consumed := make([]int, cfg.Consumers)
	var consumers sync.WaitGroup
	for c := range cfg.Consumers {
		consumers.Add(1)
		go func() {
			defer consumers.Done()
			consumed[c] = consume(ctx, cfg, c, jobs)
		}()
	}

	// The only place the channel is closed, and only after every sender has returned. Closing
	// any earlier, or from a producer, would let another producer send on a closed channel,
	// which panics. Closing is what tells the consumers' range loops to finish.
	producers.Wait()
	close(jobs)
	consumers.Wait()

	stats := Stats{PerConsumer: consumed, Elapsed: time.Since(start)}
	for p := range cfg.Producers {
		stats.Produced += produced[p]
		stats.SendWait += sendWait[p]
	}
	for _, n := range consumed {
		stats.Consumed += n
	}
	if stats.Consumed < cfg.Jobs {
		// Jobs are only ever left unconsumed because ctx was cancelled.
		return stats, ctx.Err()
	}
	return stats, nil
}

// produce sends producer id's share of the jobs (IDs id, id+Producers, id+2*Producers, ...) and
// returns how many it sent and how long it spent blocked on send.
func produce(ctx context.Context, cfg Config, id int, jobs chan<- Job) (int, time.Duration) {
	// A source per producer keeps job durations reproducible for a given seed no matter how the
	// scheduler interleaves producers, and avoids contention on a shared source.
	rng := rand.New(rand.NewPCG(cfg.Seed, uint64(id)))
	sent, wait := 0, time.Duration(0)
	for jobID := id; jobID < cfg.Jobs; jobID += cfg.Producers {
		// select picks at random among ready cases. Without this check a producer with free
		// buffer space would keep sending after cancellation half the time.
		if ctx.Err() != nil {
			return sent, wait
		}
		job := Job{ID: jobID, Producer: id, Work: workFor(rng, cfg.Work)}
		blockedAt := time.Now()
		select {
		case jobs <- job:
			wait += time.Since(blockedAt)
			sent++
		case <-ctx.Done():
			return sent, wait + time.Since(blockedAt)
		}
	}
	return sent, wait
}

// consume processes jobs until the channel is closed and drained, or ctx is cancelled, and returns
// how many jobs it finished.
func consume(ctx context.Context, cfg Config, id int, jobs <-chan Job) int {
	done := 0
	for job := range jobs {
		// After cancellation, abandon whatever is still buffered. Producers watch ctx too, so
		// none of them can stay blocked on a send that no consumer will receive.
		if ctx.Err() != nil {
			return done
		}
		if !sleep(ctx, job.Work) {
			return done // cancelled mid-job: the job did not finish, so it is not counted
		}
		done++
		if cfg.OnJob != nil {
			cfg.OnJob(id, job)
		}
	}
	return done
}

// workFor draws a simulated duration uniformly from [0, 2*mean). The mean stays at mean, while the
// spread makes consumers finish jobs at different rates, which is what shows work being shared
// through one channel rather than handed out round-robin.
func workFor(rng *rand.Rand, mean time.Duration) time.Duration {
	if mean <= 0 {
		return 0
	}
	return time.Duration(rng.Int64N(2 * int64(mean)))
}

// sleep blocks for d or until ctx is done, whichever comes first, and reports whether all of d
// elapsed.
func sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return true
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}
