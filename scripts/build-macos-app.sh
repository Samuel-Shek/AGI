#!/usr/bin/env -S -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=C LC_ALL=C /bin/sh
set -eu

ALLOW_DIRTY_BUILD=0
PROVENANCE_ONLY=0
ISOLATED_BUILD_ROOT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-dirty-development) ALLOW_DIRTY_BUILD=1 ;;
    --provenance-only) PROVENANCE_ONLY=1 ;;
    --isolated-build-root)
      if [ "$#" -lt 2 ]; then
        echo "Refusing release build: isolated build root is missing." >&2
        exit 2
      fi
      ISOLATED_BUILD_ROOT="$2"
      shift
      ;;
    *)
      echo "Refusing release build: unsupported command-line argument." >&2
      exit 2
      ;;
  esac
  shift
done

# Only an explicit argument can select private build scratch. Caller environment
# remains ignored, and cleanup removes only a newly created child, not this root.
if [ -n "$ISOLATED_BUILD_ROOT" ]; then
  case "$ISOLATED_BUILD_ROOT" in
    /*) ;;
    *) echo "Refusing release build: isolated build root must be absolute." >&2; exit 2 ;;
  esac
  ISOLATED_COMPONENT="$ISOLATED_BUILD_ROOT"
  while [ "$ISOLATED_COMPONENT" != / ]; do
    if [ -L "$ISOLATED_COMPONENT" ]; then
      echo "Refusing release build: isolated build root contains an alias." >&2
      exit 2
    fi
    ISOLATED_COMPONENT=$(/usr/bin/dirname "$ISOLATED_COMPONENT")
  done
  if [ ! -d "$ISOLATED_BUILD_ROOT" ] \
    || [ "$(/usr/bin/stat -f '%u' "$ISOLATED_BUILD_ROOT")" != "$(/usr/bin/id -u)" ] \
    || [ "$(/usr/bin/stat -f '%Lp' "$ISOLATED_BUILD_ROOT")" != 700 ]; then
    echo "Refusing release build: isolated build root must be an owner-private directory." >&2
    exit 2
  fi
fi

# This POSIX wrapper is the only supported public build entrypoint.  A
# non-interactive /bin/sh does not evaluate Bash's BASH_ENV hook.  The clean
# environment below is established before the trusted Bash implementation is
# parsed, so imported functions, SHELLOPTS, fake PATH entries, Git index
# overrides and compiler hooks cannot execute ahead of its validation.
if [ -L "$0" ]; then
  echo "Refusing release build: symlinked build entrypoint." >&2
  exit 2
fi
if ! SCRIPT_DIR=$(/usr/bin/dirname "$0"); then
  echo "Refusing release build: entrypoint directory is unavailable." >&2
  exit 2
fi
case "$SCRIPT_DIR" in
  /*) ;;
  *) SCRIPT_DIR=$(cd "$SCRIPT_DIR" && /bin/pwd -P) ;;
esac
if ! ROOT_DIR=$(cd "$SCRIPT_DIR/.." && /bin/pwd -P); then
  echo "Refusing release build: repository root is unavailable." >&2
  exit 2
fi
IMPL_PATH="$SCRIPT_DIR/build-macos-app-impl.sh"
if [ ! -f "$IMPL_PATH" ] || [ -L "$IMPL_PATH" ]; then
  echo "Refusing release build: trusted implementation is missing." >&2
  exit 2
fi
if ! IMPL_MODE=$(/usr/bin/stat -f '%Lp' "$IMPL_PATH"); then
  echo "Refusing release build: trusted implementation metadata is unavailable." >&2
  exit 2
fi
if [ "$IMPL_MODE" != "644" ]; then
  echo "Refusing release build: trusted implementation mode is invalid." >&2
  exit 2
fi

# Authenticate the Bash implementation against the current commit before Bash
# parses it.  All Git-affecting caller environment is excluded from both
# commands.
if ! IMPL_ENTRY=$(/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  DEVELOPER_DIR=/Library/Developer/CommandLineTools \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
  /usr/bin/git -C "$ROOT_DIR" ls-tree HEAD -- scripts/build-macos-app-impl.sh); then
  echo "Refusing release build: trusted implementation object is unreadable." >&2
  exit 2
fi
set -- $IMPL_ENTRY
if [ "$#" -ne 4 ] || [ "$1" != "100644" ] || [ "$2" != "blob" ] || \
   [ "$4" != "scripts/build-macos-app-impl.sh" ]; then
  echo "Refusing release build: trusted implementation is not commit-bound." >&2
  exit 2
fi
EXPECTED_IMPL_OBJECT=$3
if ! ACTUAL_IMPL_OBJECT=$(/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  DEVELOPER_DIR=/Library/Developer/CommandLineTools \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 \
  /usr/bin/git -C "$ROOT_DIR" hash-object --no-filters "$IMPL_PATH"); then
  echo "Refusing release build: trusted implementation bytes are unreadable." >&2
  exit 2
fi
if [ "$ACTUAL_IMPL_OBJECT" != "$EXPECTED_IMPL_OBJECT" ]; then
  echo "Refusing release build: trusted implementation differs from commit." >&2
  exit 2
fi

DEVELOPER_DIR_VALUE=/Library/Developer/CommandLineTools
TRUSTED_SWIFT_SDK=""
if [ "$PROVENANCE_ONLY" -eq 1 ]; then
  # Provenance-only mode hashes committed bytes and exits before invoking the
  # Swift toolchain.  Keeping toolchain selection empty also lets the same
  # commit-bound gate run in isolated fixture repositories on hosted CI.
  :
elif [ -d /Applications/Xcode_15.4.app/Contents/Developer ]; then
  DEVELOPER_DIR_VALUE=/Applications/Xcode_15.4.app/Contents/Developer
else
  # Retain the original SDK and support the installed stable CLT 26.5 SDK.
  # Fixed paths only; never accept a caller-selected SDK or moving alias.
  DEVELOPER_DIR_VALUE=/Library/Developer/CommandLineTools
  for SDK_CANDIDATE in \
    /Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk \
    /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk; do
    if [ -d "$SDK_CANDIDATE" ] && [ ! -L "$SDK_CANDIDATE" ]; then
      TRUSTED_SWIFT_SDK="$SDK_CANDIDATE"
      break
    fi
  done
  if [ -z "$TRUSTED_SWIFT_SDK" ]; then
    echo "Refusing release build: no fixed approved Xcode or CLT SDK is available." >&2
    exit 2
  fi
  for SDK_COMPONENT in \
    /Library \
    /Library/Developer \
    /Library/Developer/CommandLineTools \
    /Library/Developer/CommandLineTools/SDKs \
    "$TRUSTED_SWIFT_SDK"; do
    if [ -L "$SDK_COMPONENT" ] || \
       [ "$(/usr/bin/stat -f '%u' "$SDK_COMPONENT")" != "0" ] || \
       /usr/bin/find "$SDK_COMPONENT" -maxdepth 0 -perm -022 -print | /usr/bin/grep -q .; then
      echo "Refusing release build: fixed CLT SDK path is not root-owned and immutable." >&2
      exit 2
    fi
  done
fi

exec /usr/bin/env -i \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  LANG=C \
  LC_ALL=C \
  DEVELOPER_DIR="$DEVELOPER_DIR_VALUE" \
  PAM_TRUSTED_SWIFT_SDK="$TRUSTED_SWIFT_SDK" \
  PAM_ALLOW_DIRTY_BUILD="$ALLOW_DIRTY_BUILD" \
  PAM_PROVENANCE_ONLY="$PROVENANCE_ONLY" \
  PAM_ISOLATED_BUILD_ROOT="$ISOLATED_BUILD_ROOT" \
  /bin/bash --noprofile --norc "$IMPL_PATH"
