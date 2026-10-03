/*
 * Native tests for libsum. `make test-c` builds this with ASan and UBSan, so a
 * signed overflow or bad memory access fails the run instead of passing by
 * luck.
 */
#include <inttypes.h>
#include <stdio.h>

#include "../sum.h"

static int failures = 0;

#define EXPECT_EQ(got, want)                                                  \
  do {                                                                        \
    int64_t got_ = (int64_t)(got);                                            \
    int64_t want_ = (int64_t)(want);                                          \
    if (got_ != want_) {                                                      \
      fprintf(stderr, "%s:%d: %s = %" PRId64 ", want %" PRId64 "\n",          \
              __FILE__, __LINE__, #got, got_, want_);                         \
      ++failures;                                                             \
    }                                                                         \
  } while (0)

/* Records every progress call; optionally stops at a given index. */
struct recorder {
  int64_t calls;
  int64_t last_index;
  int64_t last_partial;
  int64_t stop_at;
  int stop_enabled;
};

static int record(int64_t index, int64_t partial_sum, void *user_data) {
  struct recorder *r = user_data;
  ++r->calls;
  r->last_index = index;
  r->last_partial = partial_sum;
  return r->stop_enabled && index == r->stop_at;
}

static void test_sums(void) {
  static const struct {
    int64_t start, stop, want;
  } cases[] = {
      {0, 0, 0},                       /* empty */
      {5, 2, 0},                       /* stop < start is empty, like range() */
      {0, 1, 0},                       /* n = 1 */
      {0, 2, 1},                       /* n = 2 */
      {0, 100, 4950},                  /* the original demo's input */
      {-3, 3, -3},                     /* crosses zero */
      {-10, 0, -55},                   /* all negative */
      {0, 1000000, INT64_C(499999500000)}, /* exceeds 32 bits */
      {INT64_MAX - 1, INT64_MAX, INT64_MAX - 1}, /* top edge, loop ends */
      {INT64_MIN, INT64_MIN + 1, INT64_MIN},     /* bottom edge */
  };
  for (size_t k = 0; k < sizeof cases / sizeof cases[0]; ++k) {
    int64_t out = -1;
    EXPECT_EQ(sum_range(cases[k].start, cases[k].stop, NULL, NULL, &out),
              SUM_OK);
    EXPECT_EQ(out, cases[k].want);
  }
}

static void test_overflow_reports_error_and_leaves_out_unchanged(void) {
  static const struct {
    int64_t start, stop;
  } cases[] = {
      {INT64_MAX - 2, INT64_MAX}, /* (MAX-2) + (MAX-1) */
      {INT64_MIN, INT64_MIN + 2}, /* MIN + (MIN+1) */
  };
  for (size_t k = 0; k < sizeof cases / sizeof cases[0]; ++k) {
    int64_t out = 42;
    EXPECT_EQ(sum_range(cases[k].start, cases[k].stop, NULL, NULL, &out),
              SUM_ERR_OVERFLOW);
    EXPECT_EQ(out, 42);
  }
}

static void test_null_out_is_invalid(void) {
  EXPECT_EQ(sum_range(0, 10, NULL, NULL, NULL), SUM_ERR_INVALID_ARG);
}

static void test_progress_sees_every_element(void) {
  struct recorder r = {0};
  int64_t out = -1;
  EXPECT_EQ(sum_range(0, 10, record, &r, &out), SUM_OK);
  EXPECT_EQ(out, 45);
  EXPECT_EQ(r.calls, 10);
  EXPECT_EQ(r.last_index, 9);
  EXPECT_EQ(r.last_partial, 45);
}

static void test_progress_can_stop_early(void) {
  struct recorder r = {.stop_at = 3, .stop_enabled = 1};
  int64_t out = -1;
  EXPECT_EQ(sum_range(0, 10, record, &r, &out), SUM_STOPPED);
  EXPECT_EQ(out, 0 + 1 + 2 + 3);
  EXPECT_EQ(r.calls, 4);
}

static void test_progress_not_called_for_empty_range(void) {
  struct recorder r = {0};
  int64_t out = -1;
  EXPECT_EQ(sum_range(3, 3, record, &r, &out), SUM_OK);
  EXPECT_EQ(r.calls, 0);
}

int main(void) {
  test_sums();
  test_overflow_reports_error_and_leaves_out_unchanged();
  test_null_out_is_invalid();
  test_progress_sees_every_element();
  test_progress_can_stop_early();
  test_progress_not_called_for_empty_range();
  if (failures != 0) {
    fprintf(stderr, "test_sum: %d failure(s)\n", failures);
    return 1;
  }
  puts("test_sum: all tests passed");
  return 0;
}
