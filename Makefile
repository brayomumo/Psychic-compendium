# Runs every prototype's own gate. Each directory is self-contained; this file
# only fans out to them. Use `make -k check` to see every failure in one run,
# or `make -j check` to run prototypes in parallel.

# Every top-level directory with a Makefile is a prototype, so adding one needs
# no change here. (templates/ holds Makefile.* files, which don't match.)
PROTOTYPES := $(patsubst %/Makefile,%,$(wildcard */Makefile))

.PHONY: check clean list $(PROTOTYPES)

check: $(PROTOTYPES)

$(PROTOTYPES):
	$(MAKE) -C $@ check

clean:
	@for dir in $(PROTOTYPES); do $(MAKE) -C $$dir clean || exit 1; done

list:
	@printf '%s\n' $(PROTOTYPES)
