package store

import (
	"context"
	"testing"
	"time"
)

func TestMemoryContract(t *testing.T) {
	runContract(t, func(*testing.T) albumStore { return NewMemory(nil) })
}

func TestMemoryUsesInjectedClock(t *testing.T) {
	at := time.Date(2026, 10, 2, 15, 47, 0, 0, time.FixedZone("EAT", 3*3600))
	s := NewMemory(func() time.Time { return at })
	a, err := s.Create(context.Background(), draft(t, "t", "a", 1))
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if !a.CreatedAt.Equal(at) || a.CreatedAt.Location() != time.UTC {
		t.Errorf("CreatedAt = %v, want %v in UTC", a.CreatedAt, at)
	}
}

func TestMemoryListReturnsACopy(t *testing.T) {
	s := NewMemory(nil)
	ctx := context.Background()
	if _, err := s.Create(ctx, draft(t, "t", "a", 1)); err != nil {
		t.Fatal(err)
	}
	page, err := s.List(ctx, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	page[0].Title = "mutated"
	again, err := s.Get(ctx, page[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	if again.Title != "t" {
		t.Errorf("caller's change leaked into the store: title %q", again.Title)
	}
}
