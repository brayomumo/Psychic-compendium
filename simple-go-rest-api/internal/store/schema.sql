-- Applied at every startup, so every statement must be idempotent.
-- The CHECK constraints repeat the domain's validation (internal/album) as a
-- second line of defence against writers that bypass the API.
CREATE TABLE IF NOT EXISTS albums (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       text        NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    artist      text        NOT NULL CHECK (char_length(artist) BETWEEN 1 AND 200),
    price_cents bigint      NOT NULL CHECK (price_cents BETWEEN 0 AND 10000000),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT albums_title_artist_key UNIQUE (title, artist)
);
