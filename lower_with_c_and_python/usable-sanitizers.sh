#!/bin/sh
# Usage: usable-sanitizers.sh CC SANITIZERS WORK_DIR
#
# Prints SANITIZERS, minus "address" if AddressSanitizer cannot run here.
# ASan hangs at startup (in FindDynamicShadowStart) with Apple clang 17 on
# macOS 26, even for an empty program; without this probe `make check` would
# hang instead of failing or degrading. perl stands in for timeout(1), which
# macOS does not ship: it runs the probe as a child and kills it after 3 s.
set -eu

cc=$1
sanitizers=$2
work_dir=$3

case ",$sanitizers," in
*,address,*) ;;
*)
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
if $cc -fsanitize=address -o "$probe" "$probe.c" >/dev/null 2>&1 &&
  perl -e "$run_with_timeout" "$probe" >/dev/null 2>&1; then
  echo "$sanitizers"
  exit 0
fi

echo "warning: AddressSanitizer does not run with $cc here; using the rest" >&2
remaining=$(echo ",$sanitizers," | sed 's/,address,/,/; s/^,//; s/,$//')
echo "${remaining:-undefined}"
