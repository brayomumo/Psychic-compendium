/*
 * libsum: a deliberately small C API for exploring a Python/C boundary.
 *
 * It follows the conventions most C libraries use at an FFI boundary:
 *   - Fixed-width integer types, so the ABI does not depend on the platform's
 *     idea of `int` or `long`.
 *   - The return value is a status code; the result goes through an out-param.
 *   - Callbacks take an opaque `user_data` pointer for caller context, and can
 *     ask the library to stop early.
 *   - The library never prints and never aborts: all outcomes are reported to
 *     the caller.
 */
#ifndef SUM_H
#define SUM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Return codes of sum_range(). Values are part of the ABI; never renumber. */
enum sum_status {
  SUM_OK = 0,
  SUM_ERR_INVALID_ARG = 1, /* `out` was NULL. */
  SUM_ERR_OVERFLOW = 2,    /* A partial sum did not fit in int64_t. */
  SUM_STOPPED = 3,         /* The progress callback asked to stop. */
};

/*
 * Called after each element is added. `index` is the element just added and
 * `partial_sum` includes it. Return 0 to continue, nonzero to stop.
 */
typedef int (*sum_progress_fn)(int64_t index, int64_t partial_sum,
                               void *user_data);

/*
 * Sums the half-open range [start, stop), like Python's sum(range(start,
 * stop)). An empty range (stop <= start) sums to 0.
 *
 * `progress` may be NULL. `user_data` is passed to `progress` untouched.
 *
 * Returns a `enum sum_status` value as an int (an enum's size is
 * implementation-defined; int is not):
 *   SUM_OK        *out holds the sum.
 *   SUM_STOPPED   *out holds the partial sum up to and including the index at
 *                 which `progress` returned nonzero.
 *   SUM_ERR_*     *out is left unchanged.
 */
int sum_range(int64_t start, int64_t stop, sum_progress_fn progress,
              void *user_data, int64_t *out);

#ifdef __cplusplus
}
#endif

#endif /* SUM_H */
