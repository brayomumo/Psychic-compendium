# libsum: a small C library meant to be called from Python with ctypes.

CC ?= cc
CFLAGS ?= -O2
SANITIZERS ?= address,undefined

# Fixed, not configurable: the Python side loads the library from here.
BUILD_DIR := build

ifeq ($(shell uname -s),Darwin)
LIB_EXT := dylib
else
LIB_EXT := so
endif
LIB := $(BUILD_DIR)/libsum.$(LIB_EXT)

IS_CLANG := $(findstring clang,$(shell $(CC) --version 2>/dev/null))
WARNINGS := -Wall -Wextra -Wpedantic -Werror
ifneq ($(IS_CLANG),)
# Not enabled by -Wall or -Wextra, and the only clang warning that flags the
# first version's uninitialised variable.
WARNINGS += -Wconditional-uninitialized
endif
STRICT_CFLAGS := -std=c11 $(WARNINGS) $(CFLAGS)

.PHONY: all build test test-c lint check clean

all: build

build: $(LIB)

$(LIB): sum.c sum.h
	@mkdir -p $(@D)
	$(CC) $(STRICT_CFLAGS) -fPIC -shared -o $@ sum.c

test: test-c

test-c:
	@mkdir -p $(BUILD_DIR) && \
	san=$$(sh usable-sanitizers.sh '$(CC)' '$(SANITIZERS)' '$(BUILD_DIR)') && \
	set -x && \
	$(CC) $(STRICT_CFLAGS) -g -fno-omit-frame-pointer \
		-fsanitize=$$san -fno-sanitize-recover=all \
		-o $(BUILD_DIR)/test_sum tests/test_sum.c sum.c && \
	$(BUILD_DIR)/test_sum

lint:
ifneq ($(IS_CLANG),)
	$(CC) --analyze -Xclang -analyzer-werror -std=c11 -o /dev/null sum.c
endif

check: lint test

clean:
	rm -rf $(BUILD_DIR)
