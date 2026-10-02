// Package album is the domain model: what an album is and which albums are
// valid. It does no I/O. Stores persist albums and the HTTP layer translates
// them to and from JSON, but the rules live here, once.
package album

import (
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// Limits on user-supplied values. They keep rows and responses bounded; the
// database schema enforces the same limits as a second line of defence.
const (
	// MaxTextRunes is the longest title or artist accepted, in runes.
	MaxTextRunes = 200
	// MaxPriceCents is the highest price accepted: 100,000.00.
	MaxPriceCents = 10_000_000
)

// Sentinel errors returned by stores. Callers match them with errors.Is.
var (
	// ErrNotFound reports that no album has the requested ID.
	ErrNotFound = errors.New("album not found")
	// ErrConflict reports that an album with the same title and artist
	// already exists.
	ErrConflict = errors.New("album already exists")
)

// Album is a stored album.
//
// Prices are integer cents. Binary floating point cannot represent most
// decimal amounts exactly (0.1 + 0.2 != 0.3), so money is never a float.
type Album struct {
	ID         int64
	Title      string
	Artist     string
	PriceCents int64
	CreatedAt  time.Time
}

// Input is the unvalidated data for a new album, as a client sent it. A nil
// PriceCents means the client did not send a price at all, which is
// different from a price of zero.
type Input struct {
	Title      string
	Artist     string
	PriceCents *int64
}

// Draft is a validated album that has not been stored yet. Its fields are
// unexported and only NewDraft creates one, so every Draft is valid.
type Draft struct {
	title      string
	artist     string
	priceCents int64
}

// Title returns the normalized title.
func (d Draft) Title() string { return d.title }

// Artist returns the normalized artist.
func (d Draft) Artist() string { return d.artist }

// PriceCents returns the price in cents.
func (d Draft) PriceCents() int64 { return d.priceCents }

// FieldError describes one invalid field. Field uses the same names as the
// HTTP API's JSON fields, so messages can be shown to clients unchanged.
type FieldError struct {
	Field   string
	Message string
}

// ValidationError lists every invalid field of an input, not just the
// first, so a client can fix them all in one round trip.
type ValidationError struct {
	Fields []FieldError
}

func (e *ValidationError) Error() string {
	parts := make([]string, len(e.Fields))
	for i, f := range e.Fields {
		parts[i] = f.Field + ": " + f.Message
	}
	return "invalid album: " + strings.Join(parts, "; ")
}

// NewDraft validates in and returns a normalized Draft: surrounding
// whitespace is trimmed from the title and artist. It returns a
// *ValidationError listing every problem when in is invalid.
func NewDraft(in Input) (Draft, error) {
	var fields []FieldError
	title, msg := normalizeText(in.Title)
	if msg != "" {
		fields = append(fields, FieldError{Field: "title", Message: msg})
	}
	artist, msg := normalizeText(in.Artist)
	if msg != "" {
		fields = append(fields, FieldError{Field: "artist", Message: msg})
	}
	var price int64
	switch {
	case in.PriceCents == nil:
		fields = append(fields, FieldError{Field: "price_cents", Message: "is required"})
	case *in.PriceCents < 0 || *in.PriceCents > MaxPriceCents:
		fields = append(fields, FieldError{
			Field:   "price_cents",
			Message: fmt.Sprintf("must be between 0 and %d", MaxPriceCents),
		})
	default:
		price = *in.PriceCents
	}
	if len(fields) > 0 {
		return Draft{}, &ValidationError{Fields: fields}
	}
	return Draft{title: title, artist: artist, priceCents: price}, nil
}

// normalizeText trims s and checks it, returning the trimmed text or a
// message describing the problem.
func normalizeText(s string) (string, string) {
	if !utf8.ValidString(s) {
		return "", "must be valid UTF-8"
	}
	s = strings.TrimSpace(s)
	switch n := utf8.RuneCountInString(s); {
	case n == 0:
		return "", "is required"
	case n > MaxTextRunes:
		return "", fmt.Sprintf("must be at most %d characters", MaxTextRunes)
	}
	if strings.IndexFunc(s, unicode.IsControl) >= 0 {
		return "", "must not contain control characters"
	}
	return s, ""
}
