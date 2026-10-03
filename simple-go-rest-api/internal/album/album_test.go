package album

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

func price(v int64) *int64 { return &v }

func TestNewDraftAcceptsAndNormalizes(t *testing.T) {
	d, err := NewDraft(Input{Title: "  Blue Train ", Artist: "John Coltrane\t", PriceCents: price(5699)})
	if err != nil {
		t.Fatalf("NewDraft: %v", err)
	}
	if d.Title() != "Blue Train" || d.Artist() != "John Coltrane" || d.PriceCents() != 5699 {
		t.Errorf("got %q / %q / %d, want trimmed values and 5699", d.Title(), d.Artist(), d.PriceCents())
	}
}

func TestNewDraftBoundaries(t *testing.T) {
	maxText := strings.Repeat("é", MaxTextRunes) // multi-byte: limits count runes, not bytes
	tests := []struct {
		name  string
		in    Input
		valid bool
	}{
		{"free album", Input{Title: "t", Artist: "a", PriceCents: price(0)}, true},
		{"max price", Input{Title: "t", Artist: "a", PriceCents: price(MaxPriceCents)}, true},
		{"max text in runes", Input{Title: maxText, Artist: maxText, PriceCents: price(1)}, true},
		{"over max price", Input{Title: "t", Artist: "a", PriceCents: price(MaxPriceCents + 1)}, false},
		{"text one rune too long", Input{Title: maxText + "x", Artist: "a", PriceCents: price(1)}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := NewDraft(tt.in)
			if (err == nil) != tt.valid {
				t.Errorf("NewDraft valid = %v, want %v (err: %v)", err == nil, tt.valid, err)
			}
		})
	}
}

// Regression: the first version accepted {"title":""} and a negative price
// with 201 Created. Every invalid field must be reported, all at once.
func TestNewDraftReportsEveryInvalidField_RegressionNoValidation(t *testing.T) {
	tests := []struct {
		name string
		in   Input
		want []FieldError
	}{
		{
			name: "empty input",
			in:   Input{},
			want: []FieldError{
				{"title", "is required"},
				{"artist", "is required"},
				{"price_cents", "is required"},
			},
		},
		{
			name: "whitespace only and negative price",
			in:   Input{Title: "   ", Artist: "a", PriceCents: price(-5)},
			want: []FieldError{
				{"title", "is required"},
				{"price_cents", "must be between 0 and 10000000"},
			},
		},
		{
			name: "control character and invalid UTF-8",
			in:   Input{Title: "bad\x00title", Artist: "\xff", PriceCents: price(1)},
			want: []FieldError{
				{"title", "must not contain control characters"},
				{"artist", "must be valid UTF-8"},
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := NewDraft(tt.in)
			var verr *ValidationError
			if !errors.As(err, &verr) {
				t.Fatalf("NewDraft error = %v, want *ValidationError", err)
			}
			if !reflect.DeepEqual(verr.Fields, tt.want) {
				t.Errorf("fields = %+v, want %+v", verr.Fields, tt.want)
			}
		})
	}
}

func TestValidationErrorMessage(t *testing.T) {
	err := &ValidationError{Fields: []FieldError{{"title", "is required"}, {"price_cents", "is required"}}}
	want := "invalid album: title: is required; price_cents: is required"
	if err.Error() != want {
		t.Errorf("Error() = %q, want %q", err.Error(), want)
	}
}
