package catalog

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// seqIDs returns an ID generator yielding id-1, id-2, ... so tests can
// predict IDs.
func seqIDs() func() (string, error) {
	var n atomic.Int64
	return func() (string, error) { return fmt.Sprintf("id-%d", n.Add(1)), nil }
}

func newTestCatalog(t *testing.T, maxProducts int) *Catalog {
	t.Helper()
	return New(maxProducts, seqIDs())
}

func mustAdd(t *testing.T, c *Catalog, name string, price int64) Product {
	t.Helper()
	p, err := c.Add(NewProduct{Name: name, Description: "about " + name, PriceCents: price}, "")
	if err != nil {
		t.Fatalf("Add(%q): %v", name, err)
	}
	return p
}

func TestAddAssignsIDAndNormalizes(t *testing.T) {
	c := newTestCatalog(t, 10)
	got, err := c.Add(NewProduct{Name: "  Kettle ", Description: " Boils water ", PriceCents: 2500}, "")
	if err != nil {
		t.Fatal(err)
	}
	want := Product{ID: "id-1", Name: "Kettle", Description: "Boils water", PriceCents: 2500}
	if got != want {
		t.Fatalf("Add = %+v, want %+v", got, want)
	}
	if stored, err := c.Get("id-1"); err != nil || stored != want {
		t.Fatalf("Get = %+v, %v; want %+v", stored, err, want)
	}
}

func TestAddReportsEveryViolation(t *testing.T) {
	tests := []struct {
		name   string
		in     NewProduct
		fields []string
	}{
		{"empty name", NewProduct{Name: "   "}, []string{"name"}},
		{"long name", NewProduct{Name: strings.Repeat("é", MaxNameLen+1)}, []string{"name"}},
		{"control char", NewProduct{Name: "bad\x07name"}, []string{"name"}},
		{"long description", NewProduct{Name: "ok", Description: strings.Repeat("x", MaxDescriptionLen+1)}, []string{"description"}},
		{"negative price", NewProduct{Name: "ok", PriceCents: -1}, []string{"price_cents"}},
		{"huge price", NewProduct{Name: "ok", PriceCents: MaxPriceCents + 1}, []string{"price_cents"}},
		{"everything wrong", NewProduct{Name: "", Description: strings.Repeat("x", MaxDescriptionLen+1), PriceCents: -5},
			[]string{"name", "description", "price_cents"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := newTestCatalog(t, 10)
			_, err := c.Add(tt.in, "")
			var verr *ValidationError
			if !errors.As(err, &verr) {
				t.Fatalf("Add error = %v, want *ValidationError", err)
			}
			var fields []string
			for _, v := range verr.Violations {
				if v.Index != -1 {
					t.Errorf("violation %+v: Index = %d, want -1 outside a batch", v, v.Index)
				}
				fields = append(fields, v.Field)
			}
			if strings.Join(fields, ",") != strings.Join(tt.fields, ",") {
				t.Fatalf("violated fields = %v, want %v", fields, tt.fields)
			}
			if c.Len() != 0 {
				t.Fatalf("Len = %d after a rejected add, want 0", c.Len())
			}
		})
	}
}

func TestAddBoundaryValuesAreAccepted(t *testing.T) {
	c := newTestCatalog(t, 10)
	for _, p := range []NewProduct{
		{Name: strings.Repeat("é", MaxNameLen), PriceCents: 0},
		{Name: "max price", Description: strings.Repeat("x", MaxDescriptionLen), PriceCents: MaxPriceCents},
	} {
		if _, err := c.Add(p, ""); err != nil {
			t.Fatalf("Add(boundary %q...): %v", p.Name[:5], err)
		}
	}
}

func TestAddRejectsDuplicateNameCaseInsensitively(t *testing.T) {
	c := newTestCatalog(t, 10)
	mustAdd(t, c, "Kettle", 100)
	_, err := c.Add(NewProduct{Name: " kETTLE "}, "")
	if !errors.Is(err, ErrAlreadyExists) {
		t.Fatalf("Add duplicate = %v, want ErrAlreadyExists", err)
	}
}

func TestAddWithRequestIDIsIdempotent(t *testing.T) {
	c := newTestCatalog(t, 10)
	p := NewProduct{Name: "Kettle", PriceCents: 100}
	first, err := c.Add(p, "req-1")
	if err != nil {
		t.Fatal(err)
	}
	// A retry, even one that differs only in surrounding whitespace, gets
	// the original product back instead of a duplicate or a name conflict.
	again, err := c.Add(NewProduct{Name: " Kettle", PriceCents: 100}, "req-1")
	if err != nil || again != first {
		t.Fatalf("retry = %+v, %v; want %+v", again, err, first)
	}
	if c.Len() != 1 {
		t.Fatalf("Len = %d after a retry, want 1", c.Len())
	}
	_, err = c.Add(NewProduct{Name: "Toaster", PriceCents: 100}, "req-1")
	if !errors.Is(err, ErrRequestIDReused) {
		t.Fatalf("reused request id = %v, want ErrRequestIDReused", err)
	}
	_, err = c.Add(NewProduct{Name: "Toaster"}, strings.Repeat("r", MaxRequestIDLen+1))
	var verr *ValidationError
	if !errors.As(err, &verr) || verr.Violations[0].Field != "request_id" {
		t.Fatalf("long request id = %v, want a request_id violation", err)
	}
}

func TestAddFailsWhenFull(t *testing.T) {
	c := newTestCatalog(t, 2)
	mustAdd(t, c, "a", 1)
	mustAdd(t, c, "b", 1)
	if _, err := c.Add(NewProduct{Name: "c"}, ""); !errors.Is(err, ErrFull) {
		t.Fatalf("Add to full catalog = %v, want ErrFull", err)
	}
}

func TestAddSurfacesIDGenerationFailure(t *testing.T) {
	boom := errors.New("entropy unavailable")
	c := New(10, func() (string, error) { return "", boom })
	if _, err := c.Add(NewProduct{Name: "a"}, ""); !errors.Is(err, boom) {
		t.Fatalf("Add = %v, want the ID generator's error", err)
	}
	collide := New(10, func() (string, error) { return "same", nil })
	mustAdd(t, collide, "first", 1)
	if _, err := collide.Add(NewProduct{Name: "second"}, ""); err == nil {
		t.Fatal("Add with a colliding ID generator succeeded, want an error")
	}
	if collide.Len() != 1 {
		t.Fatalf("Len = %d, want 1: a collision must not overwrite", collide.Len())
	}
}

func TestAddAllIsAllOrNothing(t *testing.T) {
	c := newTestCatalog(t, 10)
	mustAdd(t, c, "Existing", 1)

	_, err := c.AddAll([]NewProduct{{Name: "ok"}, {Name: ""}, {Name: "fine", PriceCents: -1}})
	var verr *ValidationError
	if !errors.As(err, &verr) {
		t.Fatalf("AddAll with invalid items = %v, want *ValidationError", err)
	}
	if got := fmt.Sprint(verr.Violations[0].Index, verr.Violations[1].Index); got != "1 2" {
		t.Fatalf("violation indexes = %s, want 1 2", got)
	}
	if _, err := c.AddAll([]NewProduct{{Name: "new"}, {Name: "existing"}}); !errors.Is(err, ErrAlreadyExists) {
		t.Fatalf("AddAll with a stored name = %v, want ErrAlreadyExists", err)
	}
	if _, err := c.AddAll([]NewProduct{{Name: "twin"}, {Name: "TWIN"}}); !errors.Is(err, ErrAlreadyExists) {
		t.Fatalf("AddAll with a duplicate inside the batch = %v, want ErrAlreadyExists", err)
	}
	if c.Len() != 1 {
		t.Fatalf("Len = %d after rejected batches, want 1", c.Len())
	}

	got, err := c.AddAll([]NewProduct{{Name: "x", PriceCents: 1}, {Name: "y", PriceCents: 2}})
	if err != nil || len(got) != 2 || got[0].Name != "x" || got[1].Name != "y" {
		t.Fatalf("AddAll = %+v, %v; want x then y", got, err)
	}
}

func TestAddAllRespectsCapacity(t *testing.T) {
	c := newTestCatalog(t, 3)
	mustAdd(t, c, "a", 1)
	if _, err := c.AddAll([]NewProduct{{Name: "b"}, {Name: "c"}, {Name: "d"}}); !errors.Is(err, ErrFull) {
		t.Fatalf("AddAll past capacity = %v, want ErrFull", err)
	}
	if c.Len() != 1 {
		t.Fatalf("Len = %d, want 1", c.Len())
	}
}

func TestAddAllRollsBackOnIDFailure(t *testing.T) {
	var calls atomic.Int64
	c := New(10, func() (string, error) {
		if calls.Add(1) == 3 {
			return "", errors.New("entropy unavailable")
		}
		return fmt.Sprintf("id-%d", calls.Load()), nil
	})
	if _, err := c.AddAll([]NewProduct{{Name: "a"}, {Name: "b"}, {Name: "c"}}); err == nil {
		t.Fatal("AddAll succeeded despite an ID failure")
	}
	if c.Len() != 0 || len(c.Search("")) != 0 {
		t.Fatalf("Len = %d after a failed batch, want 0", c.Len())
	}
	if _, err := c.Add(NewProduct{Name: "a"}, ""); err != nil {
		t.Fatalf("name from the rolled-back batch is still reserved: %v", err)
	}
}

func TestSearchMatchesNameOrDescriptionInNameOrder(t *testing.T) {
	c := newTestCatalog(t, 10)
	mustAdd(t, c, "Toaster", 1)
	mustAdd(t, c, "Kettle", 1) // description "about Kettle"
	if _, err := c.Add(NewProduct{Name: "Mug", Description: "for TEA from the kettle"}, ""); err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, p := range c.Search(" KETTLE ") {
		names = append(names, p.Name)
	}
	if got := strings.Join(names, ","); got != "Kettle,Mug" {
		t.Fatalf("Search(kettle) = %s, want Kettle,Mug", got)
	}
	if got := len(c.Search("")); got != 3 {
		t.Fatalf("Search(\"\") returned %d products, want 3", got)
	}
	if got := len(c.Search("nothing matches this")); got != 0 {
		t.Fatalf("Search(no match) returned %d products, want 0", got)
	}
}

func TestQuote(t *testing.T) {
	c := newTestCatalog(t, 10)
	p := mustAdd(t, c, "Kettle", 2500)
	maxP := mustAdd(t, c, "Expensive", MaxPriceCents)
	tests := []struct {
		name     string
		id       string
		quantity int64
		want     int64
		wantErr  error
	}{
		{"one", p.ID, 1, 2500, nil},
		{"many", p.ID, 4, 10000, nil},
		{"largest possible total", maxP.ID, MaxQuantity, MaxPriceCents * MaxQuantity, nil},
		{"zero quantity", p.ID, 0, 0, ErrInvalidQuantity},
		{"negative quantity", p.ID, -3, 0, ErrInvalidQuantity},
		{"too many", p.ID, MaxQuantity + 1, 0, ErrInvalidQuantity},
		{"unknown product", "nope", 1, 0, ErrNotFound},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := c.Quote(tt.id, tt.quantity)
			if !errors.Is(err, tt.wantErr) || got != tt.want {
				t.Fatalf("Quote(%q, %d) = %d, %v; want %d, %v", tt.id, tt.quantity, got, err, tt.want, tt.wantErr)
			}
		})
	}
}

// Regression: the first server kept products in a bare map written by
// concurrent RPCs. Run with -race; it also checks nothing was lost.
func TestConcurrentAdds_RegressionUnguardedMapRace(t *testing.T) {
	const goroutines, perGoroutine = 32, 50
	c := New(goroutines*perGoroutine, nil)
	var wg sync.WaitGroup
	for g := range goroutines {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range perGoroutine {
				name := fmt.Sprintf("product-%d-%d", g, i)
				if _, err := c.Add(NewProduct{Name: name}, ""); err != nil {
					t.Errorf("Add(%s): %v", name, err)
				}
				c.Search("product")
			}
		}()
	}
	wg.Wait()
	if got := c.Len(); got != goroutines*perGoroutine {
		t.Fatalf("Len = %d, want %d", got, goroutines*perGoroutine)
	}
	seen := map[string]bool{}
	for _, p := range c.Search("") {
		if seen[p.ID] {
			t.Fatalf("ID %s assigned twice", p.ID)
		}
		seen[p.ID] = true
	}
}

func TestConcurrentAddsOfOneNameExactlyOneWins(t *testing.T) {
	const goroutines = 64
	c := New(goroutines, nil)
	var wins atomic.Int64
	var wg sync.WaitGroup
	for range goroutines {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := c.Add(NewProduct{Name: "Contested"}, "")
			switch {
			case err == nil:
				wins.Add(1)
			case !errors.Is(err, ErrAlreadyExists):
				t.Errorf("Add = %v, want nil or ErrAlreadyExists", err)
			}
		}()
	}
	wg.Wait()
	if wins.Load() != 1 {
		t.Fatalf("%d adds of the same name succeeded, want exactly 1", wins.Load())
	}
}
