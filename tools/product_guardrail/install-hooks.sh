#!/bin/sh
# Opt-in: a pre-PUSH hook that runs the Product Guardrail.
#
#     sh tools/product_guardrail/install-hooks.sh
#
# Pre-push, deliberately not pre-commit — mid-work commits must never be
# blocked (a gate that fights the writer gets bypassed with -n and then
# forgotten). Pushing is the moment work becomes shared; that is where the
# gate belongs. Git hooks do not travel with clones, so nobody is opted in
# by someone else's machine.
#
# Escape hatch, for deliberately pushing known-red work in progress:
#     git push --no-verify
set -e
cd "$(git rev-parse --show-toplevel)"
HOOK=.git/hooks/pre-push
LINE='python3 tools/product_guardrail/check.py || exit 1'

# NEVER clobber an existing hook. This repo already ships a pre-push hook that
# runs a multi-stage gate; overwriting it would silently downgrade that to a
# one-stage guardrail run and report success while doing it.
if [ -e "$HOOK" ]; then
  if grep -qF "product_guardrail/check.py" "$HOOK"; then
    echo "already installed in $HOOK — nothing to do"
    exit 0
  fi
  # `exec` REPLACES the shell process, so anything appended after it never
  # runs. Appending under that would install a hook that silently does
  # nothing — the exact class of failure this tool exists to prevent.
  if grep -qE '^[[:space:]]*exec[[:space:]]' "$HOOK"; then
    echo "NOT INSTALLED: $HOOK already ends in an 'exec' line, so anything"
    echo "appended after it would never run. Add this line yourself, BEFORE"
    echo "that exec:"
    echo "    $LINE"
    exit 1
  fi
  cp "$HOOK" "$HOOK.bak.pre-guardrail"
  printf '\n# appended by tools/product_guardrail/install-hooks.sh\necho "Product Guardrail — git push --no-verify to skip"\n%s\n' "$LINE" >> "$HOOK"
  chmod +x "$HOOK"
  echo "APPENDED to your existing $HOOK (backup: $HOOK.bak.pre-guardrail)"
  echo "the hook that was already there still runs first"
  exit 0
fi

cat > "$HOOK" <<'INNER'
#!/bin/sh
# installed by tools/product_guardrail/install-hooks.sh
echo "Product Guardrail — git push --no-verify to skip"
exec python3 tools/product_guardrail/check.py
INNER
chmod +x "$HOOK"
echo "installed $HOOK"
echo "uninstall: rm $HOOK"
