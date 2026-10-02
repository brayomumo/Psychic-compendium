package store

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/album"
)

// albumStore is the behaviour every store must share. The HTTP layer
// declares its own interface; this one exists so one suite tests both
// implementations.
type albumStore interface {
	Create(ctx context.Context, d album.Draft) (album.Album, error)
	Get(ctx context.Context, id int64) (album.Album, error)
	List(ctx context.Context, afterID int64, limit int) ([]album.Album, error)
	Ping(ctx context.Context) error
}

func draft(t *testing.T, title, artist string, cents int64) album.Draft {
	t.Helper()
	d, err := album.NewDraft(album.Input{Title: title, Artist: artist, PriceCents: &cents})
	if err != nil {
		t.Fatalf("NewDraft(%q, %q, %d): %v", title, artist, cents, err)
	}
	return d
}

// runContract runs the shared suite. newStore must return an empty store.
func runContract(t *testing.T, newStore func(t *testing.T) albumStore) {
	ctx := context.Background()

	t.Run("create assigns increasing IDs and keeps the fields", func(t *testing.T) {
		s := newStore(t)
		before := time.Now().Add(-time.Minute)
		a1, err := s.Create(ctx, draft(t, "Blue Train", "John Coltrane", 5699))
		if err != nil {
			t.Fatalf("Create: %v", err)
		}
		a2, err := s.Create(ctx, draft(t, "Jeru", "Gerry Mulligan", 1799))
		if err != nil {
			t.Fatalf("Create: %v", err)
		}
		if a1.ID < 1 || a2.ID <= a1.ID {
			t.Errorf("IDs = %d, %d, want positive and increasing", a1.ID, a2.ID)
		}
		if a1.Title != "Blue Train" || a1.Artist != "John Coltrane" || a1.PriceCents != 5699 {
			t.Errorf("stored %+v, want the draft's fields", a1)
		}
		if a1.CreatedAt.Before(before) || a1.CreatedAt.Location() != time.UTC {
			t.Errorf("CreatedAt = %v, want a recent UTC time", a1.CreatedAt)
		}
	})

	t.Run("get returns the stored album or ErrNotFound", func(t *testing.T) {
		s := newStore(t)
		want, err := s.Create(ctx, draft(t, "Jeru", "Gerry Mulligan", 1799))
		if err != nil {
			t.Fatalf("Create: %v", err)
		}
		got, err := s.Get(ctx, want.ID)
		if err != nil {
			t.Fatalf("Get(%d): %v", want.ID, err)
		}
		if !got.CreatedAt.Equal(want.CreatedAt) {
			t.Errorf("CreatedAt = %v, want %v", got.CreatedAt, want.CreatedAt)
		}
		got.CreatedAt, want.CreatedAt = time.Time{}, time.Time{}
		if got != want {
			t.Errorf("Get = %+v, want %+v", got, want)
		}
		for _, id := range []int64{want.ID + 1, 0, -1} {
			if _, err := s.Get(ctx, id); !errors.Is(err, album.ErrNotFound) {
				t.Errorf("Get(%d) error = %v, want ErrNotFound", id, err)
			}
		}
	})

	t.Run("same title and artist conflicts", func(t *testing.T) {
		s := newStore(t)
		if _, err := s.Create(ctx, draft(t, "Jeru", "Gerry Mulligan", 1799)); err != nil {
			t.Fatalf("Create: %v", err)
		}
		if _, err := s.Create(ctx, draft(t, "Jeru", "Gerry Mulligan", 999)); !errors.Is(err, album.ErrConflict) {
			t.Errorf("duplicate Create error = %v, want ErrConflict", err)
		}
		if _, err := s.Create(ctx, draft(t, "Jeru", "Someone Else", 999)); err != nil {
			t.Errorf("same title, other artist: %v, want success", err)
		}
	})

	t.Run("list pages through albums in ID order", func(t *testing.T) {
		s := newStore(t)
		var ids []int64
		for i := range 5 {
			a, err := s.Create(ctx, draft(t, fmt.Sprintf("Album %d", i), "Artist", int64(i)))
			if err != nil {
				t.Fatalf("Create: %v", err)
			}
			ids = append(ids, a.ID)
		}
		var seen []int64
		after := int64(0)
		for range 10 { // bounded: a paging bug must not loop forever
			page, err := s.List(ctx, after, 2)
			if err != nil {
				t.Fatalf("List(%d, 2): %v", after, err)
			}
			if len(page) == 0 {
				break
			}
			for _, a := range page {
				seen = append(seen, a.ID)
			}
			after = page[len(page)-1].ID
		}
		if fmt.Sprint(seen) != fmt.Sprint(ids) {
			t.Errorf("paged IDs = %v, want %v", seen, ids)
		}
		if _, err := s.List(ctx, 0, 0); err == nil {
			t.Error("List with limit 0: want an error")
		}
	})

	// Regression: the first version appended to a shared slice from every
	// request goroutine with no lock. The race detector reported 7 races, and
	// of 20 concurrent POSTs reusing one ID, all were accepted as duplicates
	// and 3 were lost.
	t.Run("concurrent creates are safe and unique_RegressionDataRace", func(t *testing.T) {
		s := newStore(t)
		const distinct, same = 40, 20
		// Built up front: t.Fatal must not be called from other goroutines.
		drafts := make([]album.Draft, distinct+same)
		for i := range drafts {
			title := "Shared"
			if i < distinct {
				title = fmt.Sprintf("Distinct %d", i)
			}
			drafts[i] = draft(t, title, "Artist", 100)
		}
		var wg sync.WaitGroup
		ids := make(chan int64, distinct+same)
		conflicts := make(chan struct{}, same)
		errs := make(chan error, distinct+same)
		for _, d := range drafts {
			wg.Add(1)
			go func() {
				defer wg.Done()
				a, err := s.Create(ctx, d)
				switch {
				case errors.Is(err, album.ErrConflict):
					conflicts <- struct{}{}
				case err != nil:
					errs <- err
				default:
					ids <- a.ID
				}
			}()
		}
		wg.Wait()
		close(ids)
		close(errs)
		for err := range errs {
			t.Errorf("Create: %v", err)
		}
		unique := map[int64]bool{}
		for id := range ids {
			if unique[id] {
				t.Errorf("ID %d assigned twice", id)
			}
			unique[id] = true
		}
		if len(unique) != distinct+1 || len(conflicts) != same-1 {
			t.Errorf("created %d, conflicts %d; want %d and %d", len(unique), len(conflicts), distinct+1, same-1)
		}
		all, err := s.List(ctx, 0, 1000)
		if err != nil {
			t.Fatalf("List: %v", err)
		}
		if len(all) != distinct+1 {
			t.Errorf("List returned %d albums, want %d: writes were lost", len(all), distinct+1)
		}
	})

	t.Run("a cancelled context fails fast", func(t *testing.T) {
		s := newStore(t)
		cancelled, cancel := context.WithCancel(ctx)
		cancel()
		if _, err := s.Create(cancelled, draft(t, "t", "a", 1)); !errors.Is(err, context.Canceled) {
			t.Errorf("Create error = %v, want context.Canceled", err)
		}
		if _, err := s.List(cancelled, 0, 1); !errors.Is(err, context.Canceled) {
			t.Errorf("List error = %v, want context.Canceled", err)
		}
	})

	t.Run("ping succeeds", func(t *testing.T) {
		if err := newStore(t).Ping(ctx); err != nil {
			t.Errorf("Ping: %v", err)
		}
	})
}
