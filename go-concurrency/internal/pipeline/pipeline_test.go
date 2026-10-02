package pipeline

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

// recorder collects every job reported through Config.OnJob.
type recorder struct {
	mu   sync.Mutex
	jobs []Job
}

func (r *recorder) onJob(_ int, j Job) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.jobs = append(r.jobs, j)
}

func (r *recorder) snapshot() []Job {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.jobs)
}

// runWithDeadline runs Run on its own goroutine and fails the test if it has not returned within
// limit. That turns a hang, the original bug, into a clear failure instead of a stuck test binary.
//
//nolint:revive // context-as-argument: t comes first in test helpers, by convention.
func runWithDeadline(t *testing.T, ctx context.Context, cfg Config, limit time.Duration) (Stats, error) {
	t.Helper()
	type result struct {
		stats Stats
		err   error
	}
	done := make(chan result, 1)
	go func() {
		s, err := Run(ctx, cfg)
		done <- result{s, err}
	}()
	select {
	case r := <-done:
		return r.stats, r.err
	case <-time.After(limit):
		t.Fatalf("Run(%+v) did not return within %v", cfg, limit)
		return Stats{}, nil
	}
}

// waitForGoroutines fails the test unless the goroutine count drops to at most want before a
// deadline. It polls because runtime bookkeeping (such as a fired context timer) can lag briefly.
func waitForGoroutines(t *testing.T, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for runtime.NumGoroutine() > want {
		if time.Now().After(deadline) {
			buf := make([]byte, 1<<16)
			n := runtime.Stack(buf, true)
			t.Fatalf("goroutine leak: %d running, want at most %d\n%s", runtime.NumGoroutine(), want, buf[:n])
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestRunProcessesEveryJobExactlyOnce(t *testing.T) {
	t.Parallel() // runs after the sequential goroutine-leak tests, so it cannot skew their counts
	for _, producers := range []int{1, 3} {
		for _, consumers := range []int{1, 4} {
			for _, buffer := range []int{0, 1, 16} {
				for _, jobs := range []int{0, 1, 97} {
					cfg := Config{Producers: producers, Consumers: consumers, Buffer: buffer, Jobs: jobs}
					t.Run(fmt.Sprintf("p%d_c%d_b%d_j%d", producers, consumers, buffer, jobs), func(t *testing.T) {
						t.Parallel()
						var rec recorder
						cfg.OnJob = rec.onJob

						stats, err := runWithDeadline(t, context.Background(), cfg, 5*time.Second)
						if err != nil {
							t.Fatalf("Run() error = %v, want nil", err)
						}

						seen := make([]int, jobs)
						for _, j := range rec.snapshot() {
							if j.ID < 0 || j.ID >= jobs {
								t.Fatalf("job ID %d out of range [0, %d)", j.ID, jobs)
							}
							if want := j.ID % producers; j.Producer != want {
								t.Errorf("job %d came from producer %d, want %d", j.ID, j.Producer, want)
							}
							seen[j.ID]++
						}
						for id, n := range seen {
							if n != 1 {
								t.Errorf("job %d processed %d times, want exactly 1", id, n)
							}
						}

						if stats.Produced != jobs || stats.Consumed != jobs {
							t.Errorf("Produced, Consumed = %d, %d; want %d, %d", stats.Produced, stats.Consumed, jobs, jobs)
						}
						if len(stats.PerConsumer) != consumers {
							t.Fatalf("len(PerConsumer) = %d, want %d", len(stats.PerConsumer), consumers)
						}
						sum := 0
						for _, n := range stats.PerConsumer {
							sum += n
						}
						if sum != jobs {
							t.Errorf("sum(PerConsumer) = %d, want %d", sum, jobs)
						}
					})
				}
			}
		}
	}
}

// Regression: the first version gave each consumer a quit channel that was never made, so it was
// nil. Sending on a nil channel blocks forever and receiving from one never fires, so consumers
// never quit, main waited on them forever, and every quota overrun leaked a goroutine. Consumers
// now range over the jobs channel and stop when Run closes it.
func TestConsumersExitWhenChannelIsClosed_RegressionNilQuitChannel(t *testing.T) {
	before := runtime.NumGoroutine()
	cfg := Config{Producers: 1, Consumers: 2, Jobs: 100, Buffer: 10}

	stats, err := runWithDeadline(t, context.Background(), cfg, 5*time.Second)
	if err != nil {
		t.Fatalf("Run() error = %v, want nil", err)
	}
	if stats.Consumed != cfg.Jobs {
		t.Errorf("Consumed = %d, want %d", stats.Consumed, cfg.Jobs)
	}
	waitForGoroutines(t, before)
}

func TestRunWithZeroJobsReturnsImmediately(t *testing.T) {
	cfg := Config{Producers: 4, Consumers: 4, Jobs: 0, Buffer: 0, Work: MaxWork}
	stats, err := runWithDeadline(t, context.Background(), cfg, time.Second)
	if err != nil {
		t.Fatalf("Run() error = %v, want nil", err)
	}
	if stats.Produced != 0 || stats.Consumed != 0 {
		t.Errorf("Produced, Consumed = %d, %d; want 0, 0", stats.Produced, stats.Consumed)
	}
}

// More producers than jobs: the extra producers have nothing to send and must still return, or
// Run would never close the channel.
func TestRunWithMoreProducersThanJobs(t *testing.T) {
	var rec recorder
	cfg := Config{Producers: 8, Consumers: 2, Jobs: 3, Buffer: 0, OnJob: rec.onJob}
	stats, err := runWithDeadline(t, context.Background(), cfg, 5*time.Second)
	if err != nil {
		t.Fatalf("Run() error = %v, want nil", err)
	}
	if stats.Consumed != 3 || len(rec.snapshot()) != 3 {
		t.Errorf("Consumed = %d, jobs seen = %d; want 3, 3", stats.Consumed, len(rec.snapshot()))
	}
}

func TestCancellationStopsEveryGoroutine(t *testing.T) {
	// Work far longer than the test, so cancellation is the only way any of these can finish.
	const longWork = 30 * time.Second
	tests := []struct {
		name string
		cfg  Config
	}{
		{"producers blocked on unbuffered send", Config{Producers: 3, Consumers: 1, Jobs: 1000, Buffer: 0, Work: longWork}},
		{"producers blocked on full buffer", Config{Producers: 3, Consumers: 2, Jobs: 1000, Buffer: 8, Work: longWork}},
		{"consumers mid-job, nothing left to send", Config{Producers: 1, Consumers: 4, Jobs: 4, Buffer: 4, Work: longWork}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			before := runtime.NumGoroutine()
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()

			start := time.Now()
			stats, err := runWithDeadline(t, ctx, tt.cfg, 5*time.Second)
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("Run() error = %v, want context.DeadlineExceeded", err)
			}
			if elapsed := time.Since(start); elapsed > 2*time.Second {
				t.Errorf("Run took %v after a 50ms deadline, want prompt return", elapsed)
			}
			if stats.Consumed != 0 {
				t.Errorf("Consumed = %d, want 0 (no %v job can finish)", stats.Consumed, longWork)
			}
			waitForGoroutines(t, before)
		})
	}
}

// With no consumer able to finish a job, producers can get at most Consumers+Buffer jobs out:
// one in each consumer's hands and Buffer in the channel. Everything else waits in a blocked send.
// That bound is what keeps memory flat when consumers fall behind.
func TestBackpressureBoundsJobsInFlight(t *testing.T) {
	cfg := Config{Producers: 3, Consumers: 2, Jobs: 1000, Buffer: 4, Work: 30 * time.Second}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	stats, err := runWithDeadline(t, ctx, cfg, 5*time.Second)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Run() error = %v, want context.DeadlineExceeded", err)
	}
	if limit := cfg.Consumers + cfg.Buffer; stats.Produced > limit {
		t.Errorf("Produced = %d with every consumer stuck, want at most Consumers+Buffer = %d", stats.Produced, limit)
	}
	if stats.SendWait <= 0 {
		t.Errorf("SendWait = %v, want > 0: producers should have blocked", stats.SendWait)
	}
}

func TestCancelledBeforeStartConsumesNothing(t *testing.T) {
	before := runtime.NumGoroutine()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	cfg := Config{Producers: 2, Consumers: 2, Jobs: 1000, Buffer: 1000}
	stats, err := runWithDeadline(t, ctx, cfg, time.Second)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Run() error = %v, want context.Canceled", err)
	}
	if stats.Consumed != 0 {
		t.Errorf("Consumed = %d, want 0", stats.Consumed)
	}
	waitForGoroutines(t, before)
}

// An unbuffered send completes only when a consumer receives, so with one slow consumer the
// producer spends roughly the consumer's whole working time blocked. A buffer as large as the job
// count absorbs the burst instead, and the producer barely waits.
func TestBufferSizeControlsProducerBlocking(t *testing.T) {
	sendWait := func(buffer int) (wait, totalWork time.Duration) {
		var rec recorder
		cfg := Config{Producers: 1, Consumers: 1, Jobs: 10, Buffer: buffer, Work: 10 * time.Millisecond, Seed: 7, OnJob: rec.onJob}
		stats, err := runWithDeadline(t, context.Background(), cfg, 5*time.Second)
		if err != nil {
			t.Fatalf("Run() error = %v, want nil", err)
		}
		jobs := rec.snapshot()
		// The producer waits for the consumer to finish job k before it can hand over job k+1, so
		// its blocked time is at least the work of every job but the last.
		for _, j := range jobs[:len(jobs)-1] {
			totalWork += j.Work
		}
		return stats.SendWait, totalWork
	}

	unbuffered, work := sendWait(0)
	if unbuffered < work/2 {
		t.Errorf("unbuffered: producer blocked %v, want at least half of the consumer's %v of work", unbuffered, work)
	}
	buffered, work := sendWait(10)
	if buffered > work/5 {
		t.Errorf("buffer=jobs: producer blocked %v, want well under the consumer's %v of work", buffered, work)
	}
}

func TestSeedMakesJobDurationsReproducible(t *testing.T) {
	durations := func(seed uint64) map[int]time.Duration {
		var rec recorder
		cfg := Config{Producers: 3, Consumers: 4, Jobs: 50, Buffer: 5, Work: time.Microsecond, Seed: seed, OnJob: rec.onJob}
		if _, err := runWithDeadline(t, context.Background(), cfg, 5*time.Second); err != nil {
			t.Fatalf("Run() error = %v, want nil", err)
		}
		byID := make(map[int]time.Duration)
		for _, j := range rec.snapshot() {
			byID[j.ID] = j.Work
		}
		return byID
	}

	first, second, other := durations(42), durations(42), durations(43)
	for id, d := range first {
		if second[id] != d {
			t.Fatalf("seed 42: job %d work = %v then %v, want identical runs", id, d, second[id])
		}
		if d < 0 || d >= 2*time.Microsecond {
			t.Errorf("job %d work = %v, want in [0, 2µs)", id, d)
		}
	}
	differs := false
	for id, d := range first {
		if other[id] != d {
			differs = true
			break
		}
	}
	if !differs {
		t.Error("seeds 42 and 43 produced identical durations for every job")
	}
}

func TestValidate(t *testing.T) {
	valid := Config{Producers: 1, Consumers: 1, Jobs: 0, Buffer: 0, Work: 0}
	tests := []struct {
		name    string
		mutate  func(*Config)
		wantErr string // substring; empty means valid
	}{
		{"minimal", func(*Config) {}, ""},
		{"upper bounds", func(c *Config) {
			c.Producers, c.Consumers, c.Jobs, c.Buffer, c.Work = MaxWorkers, MaxWorkers, MaxJobs, MaxBuffer, MaxWork
		}, ""},
		// Regression: the first version panicked in rand.Intn(0) when consumers exceeded jobs.
		{"more consumers than jobs", func(c *Config) { c.Consumers, c.Jobs = 150, 100 }, ""},
		{"zero producers", func(c *Config) { c.Producers = 0 }, "producers"},
		{"zero consumers", func(c *Config) { c.Consumers = 0 }, "consumers"},
		// Regression: the first version panicked with "negative WaitGroup counter".
		{"negative consumers", func(c *Config) { c.Consumers = -2 }, "consumers"},
		{"too many producers", func(c *Config) { c.Producers = MaxWorkers + 1 }, "producers"},
		{"too many consumers", func(c *Config) { c.Consumers = MaxWorkers + 1 }, "consumers"},
		{"negative jobs", func(c *Config) { c.Jobs = -1 }, "jobs"},
		{"too many jobs", func(c *Config) { c.Jobs = MaxJobs + 1 }, "jobs"},
		{"negative buffer", func(c *Config) { c.Buffer = -1 }, "buffer"},
		{"huge buffer", func(c *Config) { c.Buffer = MaxBuffer + 1 }, "buffer"},
		{"negative work", func(c *Config) { c.Work = -time.Second }, "work"},
		{"huge work", func(c *Config) { c.Work = MaxWork + 1 }, "work"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := valid
			tt.mutate(&cfg)
			err := cfg.Validate()
			switch {
			case tt.wantErr == "" && err != nil:
				t.Errorf("Validate() = %v, want nil", err)
			case tt.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tt.wantErr)):
				t.Errorf("Validate() = %v, want an error mentioning %q", err, tt.wantErr)
			}
		})
	}
}

func TestValidateReportsEveryProblem(t *testing.T) {
	err := Config{Producers: 0, Consumers: 0, Jobs: -1, Buffer: -1, Work: -1}.Validate()
	if err == nil {
		t.Fatal("Validate() = nil, want errors")
	}
	for _, field := range []string{"producers", "consumers", "jobs", "buffer", "work"} {
		if !strings.Contains(err.Error(), field) {
			t.Errorf("Validate() = %q, want it to mention %q", err, field)
		}
	}
}

func TestRunRejectsInvalidConfigWithoutStartingGoroutines(t *testing.T) {
	before := runtime.NumGoroutine()
	_, err := Run(context.Background(), Config{Producers: 1, Consumers: 0})
	if err == nil || !strings.Contains(err.Error(), "consumers") {
		t.Fatalf("Run() error = %v, want an invalid-config error about consumers", err)
	}
	if after := runtime.NumGoroutine(); after > before {
		t.Errorf("goroutines went from %d to %d on a rejected config", before, after)
	}
}
