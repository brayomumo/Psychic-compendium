#include "sum.h"

#include <stddef.h>

/* True if a + b would overflow int64_t. Checked before adding, because signed
 * overflow is undefined behaviour, not wraparound: the compiler may assume it
 * never happens and delete an after-the-fact check. */
static int add_overflows(int64_t a, int64_t b) {
  return (b > 0 && a > INT64_MAX - b) || (b < 0 && a < INT64_MIN - b);
}

int sum_range(int64_t start, int64_t stop, sum_progress_fn progress,
              void *user_data, int64_t *out) {
  if (out == NULL) {
    return SUM_ERR_INVALID_ARG;
  }

  int64_t acc = 0;
  /* `i < stop` before `++i` means i + 1 <= stop <= INT64_MAX: no overflow. */
  for (int64_t i = start; i < stop; ++i) {
    if (add_overflows(acc, i)) {
      return SUM_ERR_OVERFLOW;
    }
    acc += i;
    if (progress != NULL && progress(i, acc, user_data) != 0) {
      *out = acc;
      return SUM_STOPPED;
    }
  }
  *out = acc;
  return SUM_OK;
}
