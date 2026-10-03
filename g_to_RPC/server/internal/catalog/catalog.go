// Package catalog is the in-memory product store behind the gRPC service.
//
// A gRPC server runs every RPC on its own goroutine, so everything here is
// safe for concurrent use. The first version kept products in a bare map
// that concurrent RPCs wrote to at the same time: a data race.
package catalog

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"

	"github.com/brayomumo/Psychic-compendium/g_to_RPC/server/internal/ids"
)

// Limits on what clients may store. They bound memory and keep every
// arithmetic result inside int64.
const (
	MaxNameLen         = 200               // runes
	MaxDescriptionLen  = 2000              // runes
	MaxPriceCents      = 1_000_000_000_000 // ten billion currency units
	MaxQuantity        = 1_000_000         // per quote; MaxPriceCents*MaxQuantity < 2^63
	MaxRequestIDLen    = 128               // bytes
	MaxQueryLen        = MaxNameLen        // runes
	DefaultMaxProducts = 100_000           // products held at once
	maxIDAttempts      = 3                 // UUID collisions are astronomically rare

	// The largest possible quote must fit in int64; this fails to compile
	// if a limit is raised too far.
	_ = int64(MaxPriceCents * MaxQuantity)
)

// Sentinel errors. Callers match them with errors.Is; the wrapped message
// carries the detail.
var (
	ErrNotFound        = errors.New("product not found")
	ErrAlreadyExists   = errors.New("a product with this name already exists")
	ErrRequestIDReused = errors.New("request id was already used for a different product")
	ErrFull            = errors.New("catalog is full")
	ErrInvalidQuantity = errors.New("quantity out of range")
)

// Product is a stored catalog entry.
type Product struct {
	ID          string
	Name        string
	Description string
	PriceCents  int64
}

// NewProduct is what a caller supplies to create a product.
type NewProduct struct {
	Name        string
	Description string
	PriceCents  int64
}

// FieldViolation describes one invalid field.
type FieldViolation struct {
	// Index is the position of the offending product in a batch, or -1.
	Index int
	// Field is the field name, matching the proto field name.
	Field string
	// Description says what is wrong, for a human.
	Description string
}

// ValidationError reports every invalid field of a request at once.
type ValidationError struct {
	Violations []FieldViolation
}

func (e *ValidationError) Error() string {
	parts := make([]string, len(e.Violations))
	for i, v := range e.Violations {
		parts[i] = v.Field + ": " + v.Description
		if v.Index >= 0 {
			parts[i] = fmt.Sprintf("[%d].%s", v.Index, parts[i])
		}
	}
	return "invalid product: " + strings.Join(parts, "; ")
}

type requestRecord struct {
	productID string
	product   NewProduct
}

// Catalog stores products in memory. The zero value is not usable; call New.
type Catalog struct {
	maxProducts int
	newID       func() (string, error)

	mu          sync.RWMutex
	byID        map[string]Product
	byName      map[string]string // folded name -> ID
	byRequestID map[string]requestRecord
}

// New returns an empty catalog holding at most maxProducts products. newID
// generates product IDs; nil means random UUIDs.
func New(maxProducts int, newID func() (string, error)) *Catalog {
	if newID == nil {
		newID = ids.NewUUID
	}
	return &Catalog{
		maxProducts: maxProducts,
		newID:       newID,
		byID:        make(map[string]Product),
		byName:      make(map[string]string),
		byRequestID: make(map[string]requestRecord),
	}
}

// Add stores p and returns it with its new ID. If requestID is non-empty and
// was used before for the same product, Add returns the product that call
// created, so a retried request never creates a duplicate.
func (c *Catalog) Add(p NewProduct, requestID string) (Product, error) {
	p = normalize(p)
	violations := validate(p, -1)
	if len(requestID) > MaxRequestIDLen {
		violations = append(violations, FieldViolation{Index: -1, Field: "request_id",
			Description: fmt.Sprintf("must be at most %d bytes", MaxRequestIDLen)})
	}
	if len(violations) > 0 {
		return Product{}, &ValidationError{Violations: violations}
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	if requestID != "" {
		if rec, ok := c.byRequestID[requestID]; ok {
			if rec.product != p {
				return Product{}, fmt.Errorf("%w: %q", ErrRequestIDReused, requestID)
			}
			return c.byID[rec.productID], nil
		}
	}
	if err := c.checkNewLocked([]NewProduct{p}); err != nil {
		return Product{}, err
	}
	stored, err := c.insertLocked(p)
	if err != nil {
		return Product{}, err
	}
	if requestID != "" {
		c.byRequestID[requestID] = requestRecord{productID: stored.ID, product: p}
	}
	return stored, nil
}

// AddAll stores every product in ps, or none of them: if any is invalid,
// duplicates a name, or would overflow the catalog, nothing is stored.
func (c *Catalog) AddAll(ps []NewProduct) ([]Product, error) {
	normalized := make([]NewProduct, len(ps))
	var violations []FieldViolation
	for i, p := range ps {
		normalized[i] = normalize(p)
		violations = append(violations, validate(normalized[i], i)...)
	}
	if len(violations) > 0 {
		return nil, &ValidationError{Violations: violations}
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.checkNewLocked(normalized); err != nil {
		return nil, err
	}
	stored := make([]Product, 0, len(normalized))
	for _, p := range normalized {
		s, err := c.insertLocked(p)
		if err != nil {
			// Undo, so the batch stays all-or-nothing even on an internal error.
			for _, done := range stored {
				delete(c.byID, done.ID)
				delete(c.byName, fold(done.Name))
			}
			return nil, err
		}
		stored = append(stored, s)
	}
	return stored, nil
}

// Get returns the product with the given ID.
func (c *Catalog) Get(id string) (Product, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	p, ok := c.byID[id]
	if !ok {
		return Product{}, fmt.Errorf("%w: %q", ErrNotFound, id)
	}
	return p, nil
}

// Search returns every product whose name or description contains query,
// case-insensitively, ordered by name then ID. The result is a snapshot:
// later changes do not affect it.
func (c *Catalog) Search(query string) []Product {
	q := fold(strings.TrimSpace(query))
	c.mu.RLock()
	matches := make([]Product, 0, len(c.byID))
	for _, p := range c.byID {
		if strings.Contains(fold(p.Name), q) || strings.Contains(fold(p.Description), q) {
			matches = append(matches, p)
		}
	}
	c.mu.RUnlock()
	sort.Slice(matches, func(i, j int) bool {
		if matches[i].Name != matches[j].Name {
			return matches[i].Name < matches[j].Name
		}
		return matches[i].ID < matches[j].ID
	})
	return matches
}

// Quote returns quantity times the price of the product with the given ID.
func (c *Catalog) Quote(id string, quantity int64) (int64, error) {
	if quantity < 1 || quantity > MaxQuantity {
		return 0, fmt.Errorf("%w: %d not in [1, %d]", ErrInvalidQuantity, quantity, MaxQuantity)
	}
	p, err := c.Get(id)
	if err != nil {
		return 0, err
	}
	return p.PriceCents * quantity, nil // cannot overflow: see the limits above
}

// Len returns the number of stored products.
func (c *Catalog) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.byID)
}

// checkNewLocked rejects a batch that duplicates a stored name or a name
// within itself, or that would exceed the capacity. c.mu must be held.
func (c *Catalog) checkNewLocked(ps []NewProduct) error {
	seen := make(map[string]bool, len(ps))
	for _, p := range ps {
		key := fold(p.Name)
		if _, ok := c.byName[key]; ok || seen[key] {
			return fmt.Errorf("%w: %q", ErrAlreadyExists, p.Name)
		}
		seen[key] = true
	}
	if len(c.byID)+len(ps) > c.maxProducts {
		return fmt.Errorf("%w: it holds at most %d products", ErrFull, c.maxProducts)
	}
	return nil
}

// insertLocked assigns an ID and stores p. c.mu must be held.
func (c *Catalog) insertLocked(p NewProduct) (Product, error) {
	for range maxIDAttempts {
		id, err := c.newID()
		if err != nil {
			return Product{}, fmt.Errorf("generate product id: %w", err)
		}
		if _, taken := c.byID[id]; taken {
			continue
		}
		stored := Product{ID: id, Name: p.Name, Description: p.Description, PriceCents: p.PriceCents}
		c.byID[id] = stored
		c.byName[fold(p.Name)] = id
		return stored, nil
	}
	return Product{}, errors.New("generate product id: every attempt collided with an existing id")
}

func normalize(p NewProduct) NewProduct {
	p.Name = strings.TrimSpace(p.Name)
	p.Description = strings.TrimSpace(p.Description)
	return p
}

func validate(p NewProduct, index int) []FieldViolation {
	var v []FieldViolation
	add := func(field, format string, args ...any) {
		v = append(v, FieldViolation{Index: index, Field: field, Description: fmt.Sprintf(format, args...)})
	}
	switch n := utf8.RuneCountInString(p.Name); {
	case n == 0:
		add("name", "must not be empty")
	case n > MaxNameLen:
		add("name", "must be at most %d characters, got %d", MaxNameLen, n)
	case strings.IndexFunc(p.Name, unicode.IsControl) >= 0:
		add("name", "must not contain control characters")
	}
	if n := utf8.RuneCountInString(p.Description); n > MaxDescriptionLen {
		add("description", "must be at most %d characters, got %d", MaxDescriptionLen, n)
	}
	if p.PriceCents < 0 || p.PriceCents > MaxPriceCents {
		add("price_cents", "must be in [0, %d], got %d", int64(MaxPriceCents), p.PriceCents)
	}
	return v
}

// fold is the case-insensitive key for names and searches. strings.ToLower
// uses Unicode simple case mapping, which is enough for a demo; full case
// folding (e.g. German ß) would need golang.org/x/text/cases.
func fold(s string) string { return strings.ToLower(s) }
