/*
 * The first version's sum() kept on purpose, for `make ub-demo`. NOT part of
 * the library. `sum` is never initialised, so reading it is undefined
 * behaviour: the result is whatever was in that register or stack slot, and
 * it changes with the optimisation level, the caller, and the compiler.
 *
 * Changed from the original only by dropping the per-iteration printf, which
 * was noise for this demo.
 */
#include <stdio.h>

static void ignore(int value) { (void)value; }

int sum(int num, void (*callback_type)(int));

int sum(int num, void (*callback_type)(int)) {
  int i, sum;
  for (i = 0; i < num; i++) {
    sum = sum + i;
  }
  callback_type(sum);
  return sum;
}

int main(void) {
  printf("%d\n", sum(100, ignore));
  return 0;
}
