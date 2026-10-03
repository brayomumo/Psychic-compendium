# C called from Python with ctypes. See README.md for what each target shows.

CC ?= cc
PYTHON ?= python3
RUFF ?= uvx ruff@0.16.10
MYPY ?= uvx mypy@2.4.0
CFLAGS ?= -O2
SANITIZERS ?= address,undefined
# 1: fail if ASan cannot run instead of falling back to the other sanitizers.
REQUIRE_ASAN ?= 0

# Fixed, not configurable: csum.library_path() looks for the library here.
BUILD_DIR := build

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
LIB_EXT := dylib
else
LIB_EXT := so
endif
LIB := $(BUILD_DIR)/libsum.$(LIB_EXT)

IS_CLANG := $(findstring clang,$(shell $(CC) --version 2>/dev/null))
WARNINGS := -Wall -Wextra -Wpedantic -Werror
ifneq ($(IS_CLANG),)
# Not enabled by -Wall or -Wextra, and the only clang warning that flags the
# first version's uninitialised variable (see `make ub-demo`).
WARNINGS += -Wconditional-uninitialized
endif
STRICT_CFLAGS := -std=c11 $(WARNINGS) $(CFLAGS)

# MemorySanitizer: clang on Linux only. Origin tracking reports where an
# uninitialised value came from. test-msan adds -O1, as the MSan docs advise.
MSAN_FLAGS := -fsanitize=memory -fsanitize-memory-track-origins=2 \
	-fno-omit-frame-pointer -g
HAS_MSAN := $(and $(IS_CLANG),$(filter Linux,$(UNAME_S)))

.PHONY: all build run bench ub-demo test test-c test-msan test-py lint check \
	clean

all: build

build: $(LIB)

$(LIB): sum.c sum.h
	@mkdir -p $(@D)
	$(CC) $(STRICT_CFLAGS) -fPIC -shared -o $@ sum.c

run: build
	$(PYTHON) main.py

bench: build
	$(PYTHON) bench.py

test: test-c test-py

test-c:
	@mkdir -p $(BUILD_DIR) && \
	san=$$(sh usable-sanitizers.sh '$(CC)' '$(SANITIZERS)' '$(BUILD_DIR)' \
		'$(REQUIRE_ASAN)') && \
	set -x && \
	$(CC) $(STRICT_CFLAGS) -g -fno-omit-frame-pointer \
		-fsanitize=$$san -fno-sanitize-recover=all \
		-o $(BUILD_DIR)/test_sum tests/test_sum.c sum.c && \
	$(BUILD_DIR)/test_sum

# Not part of `test`: MSan exists only for clang on Linux. CI runs it there.
test-msan:
ifeq ($(HAS_MSAN),)
	@echo "test-msan needs clang on Linux (CC=$(CC), OS=$(UNAME_S))" >&2; exit 2
else
	@mkdir -p $(BUILD_DIR)
	$(CC) -std=c11 $(WARNINGS) -O1 $(MSAN_FLAGS) \
		-o $(BUILD_DIR)/test_sum_msan tests/test_sum.c sum.c
	$(BUILD_DIR)/test_sum_msan
endif

test-py: build
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) .
ifneq ($(IS_CLANG),)
	$(CC) --analyze -Xclang -analyzer-werror -std=c11 -o /dev/null sum.c
endif

check: lint test

# Reproduces the first version's undefined behaviour and shows which tools
# catch it. Results vary by machine and compiler; that is the point.
ub-demo:
ifeq ($(IS_CLANG),)
	@echo "ub-demo uses clang-only flags; set CC to a clang" >&2; exit 2
else
	@mkdir -p $(BUILD_DIR)
	@echo "Warnings on ub_demo.c (the first version's code):"
	@for flags in "-Wall -Wextra" "-Wall -Wextra -Wconditional-uninitialized"; do \
		n=$$($(CC) -std=c11 $$flags -c -o /dev/null ub_demo.c 2>&1 | grep -c 'warning:'); \
		printf '  %-44s %s\n' "$$flags" "$$n warning(s)"; \
	done
	@n=$$($(CC) --analyze -o /dev/null ub_demo.c 2>&1 | grep -c 'warning:'); \
		printf '  %-44s %s\n' "--analyze (static analyzer)" "$$n warning(s)"
	@echo "Results (correct answer: 4950):"
	@for flags in -O0 -O2 "-O2 -fsanitize=undefined" \
		"-O2 -ftrivial-auto-var-init=pattern" "-O2 -ftrivial-auto-var-init=zero"; do \
		$(CC) -std=c11 $$flags -o $(BUILD_DIR)/ub_demo ub_demo.c 2>/dev/null && \
		printf '  %-44s %s\n' "$$flags" "$$($(BUILD_DIR)/ub_demo 2>&1 | tr '\n' ' ')"; \
	done
ifneq ($(HAS_MSAN),)
	@# -O0 keeps `sum` out of a register, so MSan can name where it is read.
	@$(CC) -std=c11 -O0 $(MSAN_FLAGS) -o $(BUILD_DIR)/ub_demo_msan ub_demo.c && \
	out=$$($(BUILD_DIR)/ub_demo_msan 2>&1); status=$$?; \
	report=$$(printf '%s\n' "$$out" | \
		grep -m 2 -oE 'MemorySanitizer: [a-z-]+|in [a-z_]+ [^ ]*ub_demo\.c:[0-9]+' | \
		sed 's|in \([a-z_]*\) .*/|in \1() |' | tr '\n' ' '); \
	if [ -n "$$report" ]; then :; \
	elif [ $$status -eq 0 ]; then report="not detected: printed $$out"; \
	else report="MSan failed to run (exit $$status; run $(BUILD_DIR)/ub_demo_msan)"; \
	fi; \
	printf '  %-44s %s\n' "-O0 -fsanitize=memory" "$$report"
endif
endif

clean:
	rm -rf $(BUILD_DIR) .mypy_cache .ruff_cache __pycache__ tests/__pycache__
