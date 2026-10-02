// Package store persists albums. Memory keeps them in process for tests and
// quick local runs; Postgres keeps them in PostgreSQL. Both implement the
// same contract, which the tests check against each.
package store

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/album"
)

// Memory is an in-memory album store, safe for concurrent use. (The first
// version mutated a package-level slice from every request goroutine with
// no lock, a data race that lost writes.)
type Memory struct {
	now func() time.Time

	mu     sync.RWMutex
	nextID int64
	albums []album.Album // ordered by ID, because IDs only ever increase
	keys   map[uniqueKey]struct{}
}

// uniqueKey mirrors the UNIQUE (title, artist) constraint of the schema.
type uniqueKey struct{ title, artist string }

// NewMemory returns an empty store. now supplies creation times; nil means
// time.Now.
func NewMemory(now func() time.Time) *Memory {
	if now == nil {
		now = time.Now
	}
	return &Memory{now: now, nextID: 1, keys: make(map[uniqueKey]struct{})}
}

// Create stores d under the next ID. It returns album.ErrConflict if an
// album with the same title and artist exists.
func (m *Memory) Create(ctx context.Context, d album.Draft) (album.Album, error) {
	if err := ctx.Err(); err != nil {
		return album.Album{}, err
	}
	key := uniqueKey{d.Title(), d.Artist()}
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, exists := m.keys[key]; exists {
		return album.Album{}, album.ErrConflict
	}
	a := album.Album{
		ID:         m.nextID,
		Title:      d.Title(),
		Artist:     d.Artist(),
		PriceCents: d.PriceCents(),
		CreatedAt:  m.now().UTC(),
	}
	m.nextID++
	m.albums = append(m.albums, a)
	m.keys[key] = struct{}{}
	return a, nil
}

// Get returns the album with the given ID, or album.ErrNotFound.
func (m *Memory) Get(ctx context.Context, id int64) (album.Album, error) {
	if err := ctx.Err(); err != nil {
		return album.Album{}, err
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	i := m.indexAfter(id - 1)
	if i == len(m.albums) || m.albums[i].ID != id {
		return album.Album{}, album.ErrNotFound
	}
	return m.albums[i], nil
}

// List returns up to limit albums with IDs greater than afterID, in ID
// order. limit must be positive.
func (m *Memory) List(ctx context.Context, afterID int64, limit int) ([]album.Album, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if limit < 1 {
		return nil, fmt.Errorf("store: limit must be positive, got %d", limit)
	}
	m.mu.RLock()
	defer m.mu.RUnlock()
	start := m.indexAfter(afterID)
	end := min(start+limit, len(m.albums))
	// A copy: callers must not see later appends or alias the store's array.
	return append([]album.Album(nil), m.albums[start:end]...), nil
}

// Ping reports whether the store can serve requests; memory always can.
func (m *Memory) Ping(ctx context.Context) error { return ctx.Err() }

// indexAfter returns the index of the first album with ID > afterID. The
// caller holds m.mu.
func (m *Memory) indexAfter(afterID int64) int {
	return sort.Search(len(m.albums), func(i int) bool { return m.albums[i].ID > afterID })
}
