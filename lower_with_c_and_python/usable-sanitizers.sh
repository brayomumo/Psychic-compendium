#!/bin/sh
# Usage: usable-sanitizers.sh CC SANITIZERS WORK_DIR REQUIRE_ASAN
#
# Prints the -fsanitize= list to build the native test with.
#
# If SANITIZERS includes "address", a tiny ASan program is built and run
# first. ASan hangs at startup (in FindDynamicShadowStart) with Apple clang 17
# on macOS 26, even for an empty program; without this probe `make check`
# would hang. When the probe fails:
#   REQUIRE_ASAN=0  warn on stderr and print SANITIZERS without "address".
#   REQUIRE_ASAN=1  print why on stderr and exit 1. CI uses this so a broken
#                   ASan can never pass silently as a UBSan-only run.
# Exit status 2 means a usage error.
#
# perl stands in for timeout(1), which macOS does not ship: it runs the probe
# as a child and kills it after 3 s. perl-base is part of every Debian/Ubuntu
# install, including the ubuntu:24.04 container image.
set -eu

if [ $# -ne 4 ]; then
  echo "usage: $0 CC SANITIZERS WORK_DIR REQUIRE_ASAN" >&2
  exit 2
fi
cc=$1
sanitizers=$2
work_dir=$3
require_asan=$4

case $require_asan in
0 | 1) ;;
*)
  echo "error: REQUIRE_ASAN must be 0 or 1, got '$require_asan'" >&2
  exit 2
  ;;
esac

case ",$sanitizers," in
*,address,*) ;;
*)
  if [ "$require_asan" = 1 ]; then
    echo "error: REQUIRE_ASAN=1 but SANITIZERS='$sanitizers' has no 'address'" >&2
    exit 2
  fi
  echo "$sanitizers"
  exit 0
  ;;
esac

probe="$work_dir/asan_probe"
printf 'int main(void) { return 0; }\n' >"$probe.c"
# shellcheck disable=SC2016 # $pid and $? are perl variables.
run_with_timeout='
  my $pid = fork // die "fork: $!";
  if ($pid == 0) { exec @ARGV or exit 127 }
  $SIG{ALRM} = sub { kill "KILL", $pid; exit 124 };
  alarm 3;
  waitpid $pid, 0;
  exit($? == 0 ? 0 : 1);
'
# $cc is unquoted on purpose: CC may carry a launcher, e.g. "ccache clang".
if ! $cc -fsanitize=address -o "$probe" "$probe.c" >"$probe.log" 2>&1; then
  reason="cannot build with -fsanitize=address: $(head -n 1 "$probe.log")"
elif perl -e "$run_with_timeout" "$probe" >"$probe.log" 2>&1; then
  echo "$sanitizers"
  exit 0
elif [ $? -eq 124 ]; then
  reason="an empty ASan program hung for 3 s at startup"
else
  reason="an empty ASan program failed: $(head -n 1 "$probe.log")"
fi

if [ "$require_asan" = 1 ]; then
  echo "error: REQUIRE_ASAN=1 but AddressSanitizer does not work with" \
    "$cc here: $reason" >&2
  exit 1
fi
echo "warning: AddressSanitizer does not work with $cc here ($reason);" \
  "using the rest. Set REQUIRE_ASAN=1 to make this an error." >&2
remaining=$(echo ",$sanitizers," | sed 's/,address,/,/; s/^,//; s/,$//')
echo "${remaining:-undefined}"
