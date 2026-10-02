# syntax=docker/dockerfile:1

# Build stage: compile a static binary. The Go version satisfies go.mod.
FROM golang:1.23-alpine AS build
WORKDIR /src
# Dependencies first, so editing code does not invalidate this layer.
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/albums ./cmd/albums

# Runtime stage: no shell or package manager, runs as an unprivileged user.
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/albums /albums
EXPOSE 8080
# Exec form: albums runs as PID 1 and receives SIGTERM from `docker stop`
# directly, so it can drain in-flight requests before exiting.
ENTRYPOINT ["/albums"]
CMD ["-addr", ":8080", "-store", "postgres"]
