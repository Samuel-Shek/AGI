#!/bin/bash
set -euo pipefail
umask 022
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_NO_REPLACE_OBJECTS=1

ROOT_DIR="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")/.." && /bin/pwd -P)"

capture_xcode_identity() {
  local xcode_app="/Applications/Xcode_15.4.app"
  local xcode_info="$xcode_app/Contents/Info.plist"
  local expected_swift="$DEVELOPER_DIR/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift"
  local expected_sdk="$DEVELOPER_DIR/Platforms/MacOSX.platform/Developer/SDKs/MacOSX14.5.sdk"
  local sdk_settings="$expected_sdk/SDKSettings.json"
  local bundle_id bundle_version xcode_build xcode_version swift_path swift_version
  local sdk_path sdk_version info_hash swift_hash sdk_settings_hash identity identity_file
  local -a version_mismatches=()

  if ! /usr/bin/codesign --verify --deep --strict "$xcode_app" >/dev/null 2>&1 \
    || ! /usr/bin/codesign --verify --strict \
      -R='identifier "com.apple.dt.Xcode" and anchor apple' \
      "$xcode_app" >/dev/null 2>&1; then
    echo "Refusing release build: Xcode Apple signature is invalid." >&2
    return 1
  fi
  for identity_file in "$xcode_info" "$sdk_settings"; do
    if [[ ! -f "$identity_file" || -L "$identity_file" ]]; then
      echo "Refusing release build: Xcode identity file is not a regular file." >&2
      return 1
    fi
  done
  bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$xcode_info")"
  bundle_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$xcode_info")"
  xcode_build="$(/usr/libexec/PlistBuddy -c 'Print :DTXcodeBuild' "$xcode_info")"
  xcode_version="$(/usr/bin/xcodebuild -version)"
  [[ "$bundle_id" == "com.apple.dt.Xcode" ]] \
    || version_mismatches+=("bundle-identifier")
  [[ "$bundle_version" == "15.4" ]] \
    || version_mismatches+=("bundle-version")
  # Apple's signed Xcode 15.4 app bundle reports DTXcodeBuild 15F31c while
  # xcodebuild reports the public release build 15F31d.  Seal both identities
  # independently instead of conflating the private bundle field with the
  # public command-line release identity.
  [[ "$xcode_build" == "15F31c" ]] \
    || version_mismatches+=("bundle-build")
  [[ "$xcode_version" == $'Xcode 15.4\nBuild version 15F31d' ]] \
    || version_mismatches+=("xcodebuild-version")
  if [[ ${#version_mismatches[@]} -ne 0 ]]; then
    echo "Refusing release build: Xcode version identity is invalid (${version_mismatches[*]})." >&2
    return 1
  fi

  swift_path="$(/usr/bin/xcrun --find swift)"
  sdk_path="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
  sdk_version="$(/usr/bin/xcrun --sdk macosx --show-sdk-version)"
  if [[ "$swift_path" != "$expected_swift" \
    || "$sdk_path" != "$expected_sdk" \
    || "$sdk_version" != "14.5" ]]; then
    echo "Refusing release build: selected Xcode toolchain path is invalid." >&2
    return 1
  fi
  if ! /usr/bin/codesign --verify --strict -R='anchor apple' \
      "$swift_path" >/dev/null 2>&1; then
    echo "Refusing release build: selected Swift compiler signature is invalid." >&2
    return 1
  fi
  # Xcode 15.4 writes the swift-driver identity to stderr first, without a
  # newline, then writes the Apple Swift and target identity to stdout. Capture
  # both channels so the complete signed-compiler byte sequence is compared
  # rather than silently accepting a truncated stdout-only version string.
  if ! swift_version="$("$swift_path" --version 2>&1)" \
    || [[ "$swift_version" != $'swift-driver version: 1.90.11.1 Apple Swift version 5.10 (swiftlang-5.10.0.13 clang-1500.3.9.4)\nTarget: arm64-apple-macosx14.0' ]]; then
    echo "Refusing release build: selected Swift compiler version is invalid." >&2
    printf 'Observed Swift version identity: %q\n' "$swift_version" >&2
    return 1
  fi

  if ! info_hash="$(/usr/bin/shasum -a 256 "$xcode_info" | /usr/bin/awk '{print $1}')" \
    || [[ ! "$info_hash" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Refusing release build: Xcode Info.plist identity is unreadable." >&2
    return 1
  fi
  if ! swift_hash="$(/usr/bin/shasum -a 256 "$swift_path" | /usr/bin/awk '{print $1}')" \
    || [[ ! "$swift_hash" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Refusing release build: Swift compiler identity is unreadable." >&2
    return 1
  fi
  if ! sdk_settings_hash="$(/usr/bin/shasum -a 256 "$sdk_settings" | /usr/bin/awk '{print $1}')" \
    || [[ ! "$sdk_settings_hash" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Refusing release build: Xcode SDK identity is unreadable." >&2
    return 1
  fi
  identity="$bundle_id|$bundle_version|$xcode_build|$xcode_version|$swift_path|$swift_version|$sdk_path|$sdk_version|$info_hash|$swift_hash|$sdk_settings_hash"
  printf '%s' "$identity" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}'
}

for injected_name in \
  PAM_SWIFT_SDK SDKROOT TOOLCHAINS SWIFT_EXEC \
  SWIFT_DRIVER_SWIFT_FRONTEND_EXEC SWIFT_DRIVER_DSYMUTIL_EXEC \
  CC CXX LD AR CFLAGS CXXFLAGS LDFLAGS SWIFTFLAGS; do
  if [[ -n "${!injected_name-}" ]]; then
    echo "Refusing release build: compiler environment injection is set ($injected_name)." >&2
    exit 2
  fi
done

if [[ "${DEVELOPER_DIR-}" == "/Library/Developer/CommandLineTools" ]]; then
  for developer_component in /Library /Library/Developer /Library/Developer/CommandLineTools; do
    if [[ ! -d "$developer_component" || -L "$developer_component" ]] \
      || [[ "$(/usr/bin/stat -f '%u' "$developer_component")" != 0 ]] \
      || [[ -n "$(/usr/bin/find "$developer_component" -maxdepth 0 -perm -022 -print)" ]]; then
      echo "Refusing release build: untrusted CLT developer directory." >&2
      exit 2
    fi
  done
elif [[ -n "${DEVELOPER_DIR-}" ]]; then
  if [[ "$DEVELOPER_DIR" != "/Applications/Xcode_15.4.app/Contents/Developer" ]]; then
    echo "Refusing release build: unapproved DEVELOPER_DIR." >&2
    exit 2
  fi
  trusted_developer_owner_uid="0"
  current_user="$(/usr/bin/id -un)"
  current_uid="$(/usr/bin/id -u)"
  if [[ "$current_user" == "runner" \
    && "$current_uid" == "501" \
    && "$ROOT_DIR" == "/Users/runner/work/AGI/AGI" \
    && "$(/usr/bin/uname -s)" == "Darwin" \
    && "$(/usr/bin/git -C "$ROOT_DIR" config --get remote.origin.url)" == "https://github.com/Samuel-Shek/AGI" ]]; then
    # GitHub's pinned macOS hosted image owns the preinstalled Xcode bundle as
    # its ephemeral `runner` account.  Accept that exact image layout only;
    # local users cannot opt in with caller-controlled environment variables
    # because the public wrapper starts under env -i.
    trusted_developer_owner_uid="$current_uid"
  fi
  developer_component="/Applications"
  for suffix in Xcode_15.4.app Contents Developer; do
    developer_component="$developer_component/$suffix"
    [[ -d "$developer_component" && ! -L "$developer_component" ]]
    read -r component_uid component_mode <<< "$(/usr/bin/stat -f '%u %Lp' "$developer_component")"
    if [[ "$component_uid" != "0" \
      && "$component_uid" != "$trusted_developer_owner_uid" ]] \
      || [[ $((8#$component_mode & 8#022)) -ne 0 ]]; then
      echo "Refusing release build: untrusted DEVELOPER_DIR component ($developer_component uid=$component_uid mode=$component_mode)." >&2
      exit 2
    fi
  done
  if ! XCODE_IDENTITY_BEFORE="$(capture_xcode_identity)" \
    || [[ ! "$XCODE_IDENTITY_BEFORE" =~ ^[0-9a-f]{64}$ ]]; then
    exit 2
  fi
fi

APP_DIR="$ROOT_DIR/build/AGI.app"
STAGING_APP_DIR="$ROOT_DIR/build/.AGI.app.staging.$$"
SWIFT_DIR="$ROOT_DIR/apps/PersonalAIMonitorMenuBar"
ICNS_FILE="$ROOT_DIR/build/AppIcon.icns"
MARKETING_VERSION="0.9.1"
BUILD_NUMBER="$(/bin/date +%Y%m%d%H%M)"
BUILD_TIMESTAMP="$(/bin/date '+%Y-%m-%d %H:%M:%S %z')"
ALLOW_DIRTY_BUILD="${PAM_ALLOW_DIRTY_BUILD:-0}"
PROVENANCE_ONLY="${PAM_PROVENANCE_ONLY:-0}"
SWIFT_SDK_PATH="${PAM_TRUSTED_SWIFT_SDK:-}"
if [[ -n "$SWIFT_SDK_PATH" \
  && "$SWIFT_SDK_PATH" != "/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk" \
  && "$SWIFT_SDK_PATH" != "/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk" ]]; then
  echo "Refusing release build: internal Swift SDK selection is invalid." >&2
  exit 2
fi
if [[ -n "${PAM_ISOLATED_BUILD_ROOT:-}" ]]; then
  EPHEMERAL_BUILD_ROOT="$(/usr/bin/mktemp -d "$PAM_ISOLATED_BUILD_ROOT/pam-v040-build.XXXXXX")"
else
  EPHEMERAL_BUILD_ROOT="$(/usr/bin/mktemp -d /private/tmp/pam-v040-build.XXXXXX)"
fi
SWIFT_CACHE_ROOT="$EPHEMERAL_BUILD_ROOT/swift-cache"
SWIFT_SCRATCH_PATH="$EPHEMERAL_BUILD_ROOT/swift-scratch"
export HOME="$EPHEMERAL_BUILD_ROOT/home"
export CFFIXED_USER_HOME="$HOME"
export TMPDIR="$EPHEMERAL_BUILD_ROOT/tmp"
/bin/mkdir -p "$TMPDIR"
SWIFT_BUILD_ARGS=(
  --build-system native
  --package-path "$SWIFT_DIR"
  --disable-sandbox
  --cache-path "$SWIFT_CACHE_ROOT/cache"
  --config-path "$SWIFT_CACHE_ROOT/config"
  --security-path "$SWIFT_CACHE_ROOT/security"
  --scratch-path "$SWIFT_SCRATCH_PATH"
  --manifest-cache none
)
if [[ -n "$SWIFT_SDK_PATH" ]]; then
  SWIFT_BUILD_ARGS+=(--sdk "$SWIFT_SDK_PATH")
fi
export CLANG_MODULE_CACHE_PATH="$SWIFT_CACHE_ROOT/clang-module-cache"

cleanup_staging() {
  /bin/rm -rf "$STAGING_APP_DIR"
  /bin/rm -rf "$EPHEMERAL_BUILD_ROOT"
}
trap cleanup_staging EXIT

compute_source_tree_hash() {
  local listing="$EPHEMERAL_BUILD_ROOT/source-tree.list"
  local manifest="$EPHEMERAL_BUILD_ROOT/source-tree.manifest"
  local entry metadata relative_path mode object_type object_id object_sha
  local entry_count=0
  : > "$manifest"
  if ! /usr/bin/git -C "$ROOT_DIR" ls-tree -r -z --full-tree \
      "$GIT_FULL_REVISION" > "$listing"; then
    echo "Refusing release build: source Git tree is unreadable." >&2
    return 1
  fi
  while IFS= read -r -d '' entry; do
    entry_count=$((entry_count + 1))
    metadata="${entry%%$'\t'*}"
    relative_path="${entry#*$'\t'}"
    read -r mode object_type object_id <<< "$metadata"
    if [[ "$object_type" != "blob" ]] || \
       [[ "$mode" != "100644" && "$mode" != "100755" ]]; then
      echo "Refusing release build: source Git tree has a special entry." >&2
      return 1
    fi
    if ! object_sha="$(/usr/bin/git -C "$ROOT_DIR" cat-file blob "$object_id" \
        | /usr/bin/shasum -a 256 | /usr/bin/awk '{ print $1 }')"; then
      echo "Refusing release build: source Git object is unreadable." >&2
      return 1
    fi
    if [[ ! "$object_sha" =~ ^[0-9a-f]{64}$ ]]; then
      echo "Refusing release build: source Git object identity is invalid." >&2
      return 1
    fi
    printf '%s\0%s\n' "$relative_path" "$object_sha" >> "$manifest"
  done < "$listing"
  if [[ "$entry_count" -eq 0 ]]; then
    echo "Refusing release build: source Git tree is empty." >&2
    return 1
  fi
  /usr/bin/shasum -a 256 "$manifest" | /usr/bin/awk '{ print $1 }'
}

compute_core_code_hash() {
  local listing="$EPHEMERAL_BUILD_ROOT/core-tree.list"
  local manifest="$EPHEMERAL_BUILD_ROOT/core-tree.manifest"
  local entry metadata relative_path mode object_type object_id object_sha
  local entry_count=0 selected_count=0
  : > "$manifest"
  if ! /usr/bin/git -C "$ROOT_DIR" ls-tree -r -z --full-tree \
      "$GIT_FULL_REVISION" -- core > "$listing"; then
    echo "Refusing release build: Core Git tree is unreadable." >&2
    return 1
  fi
  while IFS= read -r -d '' entry; do
    entry_count=$((entry_count + 1))
    metadata="${entry%%$'\t'*}"
    relative_path="${entry#*$'\t'}"
    read -r mode object_type object_id <<< "$metadata"
    if [[ "$object_type" != "blob" ]] || \
       [[ "$mode" != "100644" && "$mode" != "100755" ]]; then
      echo "Refusing release build: Core Git tree has a special entry." >&2
      return 1
    fi
    if [[ "$relative_path" != "core/pyproject.toml" \
      && "$relative_path" != "core/pam_isolated_launcher.py" \
      && "$relative_path" != core/activity_mvp/* \
      && "$relative_path" != core/tools/* \
      && "$relative_path" != core/browser_extension/* \
      && "$relative_path" != core/evolution/* ]]; then
      continue
    fi
    selected_count=$((selected_count + 1))
    if ! object_sha="$(/usr/bin/git -C "$ROOT_DIR" cat-file blob "$object_id" \
        | /usr/bin/shasum -a 256 | /usr/bin/awk '{ print $1 }')"; then
      echo "Refusing release build: Core Git object is unreadable." >&2
      return 1
    fi
    if [[ ! "$object_sha" =~ ^[0-9a-f]{64}$ ]]; then
      echo "Refusing release build: Core Git object identity is invalid." >&2
      return 1
    fi
    printf '%s\0%s\n' "$relative_path" "$object_sha" >> "$manifest"
  done < "$listing"
  if [[ "$entry_count" -eq 0 || "$selected_count" -eq 0 ]]; then
    echo "Refusing release build: Core Git tree is empty." >&2
    return 1
  fi
  /usr/bin/shasum -a 256 "$manifest" | /usr/bin/awk '{ print $1 }'
}

git_status_exact() {
  /usr/bin/git -C "$ROOT_DIR" \
    -c core.fsmonitor=false \
    -c core.untrackedCache=false \
    status --porcelain=v1 --untracked-files=normal
}

assert_git_controls() {
  local replace_refs unmerged
  if ! replace_refs="$(/usr/bin/git -C "$ROOT_DIR" for-each-ref --format='%(refname)' refs/replace)" \
     || [[ -n "$replace_refs" ]]; then
    echo "Refusing release build: Git replace refs are active." >&2
    return 1
  fi
  if ! unmerged="$(/usr/bin/git -C "$ROOT_DIR" ls-files -u -z)" \
     || [[ -n "$unmerged" ]]; then
    echo "Refusing release build: Git index is unmerged." >&2
    return 1
  fi
  local grafts_path
  if ! grafts_path="$(/usr/bin/git -C "$ROOT_DIR" rev-parse --git-path info/grafts)"; then
    echo "Refusing release build: Git graft path is unreadable." >&2
    return 1
  fi
  if [[ "$grafts_path" != /* ]]; then grafts_path="$ROOT_DIR/$grafts_path"; fi
  if [[ -e "$grafts_path" || -L "$grafts_path" ]]; then
    echo "Refusing release build: Git grafts are active." >&2
    return 1
  fi
  local index_listing="$EPHEMERAL_BUILD_ROOT/index-flags.list"
  if ! /usr/bin/git -C "$ROOT_DIR" ls-files -v -z > "$index_listing"; then
    echo "Refusing release build: Git index flags are unreadable." >&2
    return 1
  fi
  local unsafe_index=0
  while IFS= read -r -d '' index_row; do
    if [[ "${index_row:0:2}" != "H " ]]; then
      unsafe_index=1
    fi
  done < "$index_listing"
  if [[ "$unsafe_index" != "0" ]]; then
    echo "Refusing release build: unsafe Git index flags." >&2
    return 1
  fi
}

assert_worktree_matches_commit() {
  local entry metadata relative_path mode object_type object_id path actual_mode
  local expected_sha actual_sha
  local listing="$EPHEMERAL_BUILD_ROOT/worktree-tree.list"
  local entry_count=0
  if ! /usr/bin/git -C "$ROOT_DIR" ls-tree -r -z --full-tree \
      "$GIT_FULL_REVISION" > "$listing"; then
    echo "Refusing release build: worktree Git tree is unreadable." >&2
    return 1
  fi
  while IFS= read -r -d '' entry; do
    entry_count=$((entry_count + 1))
    metadata="${entry%%$'\t'*}"
    relative_path="${entry#*$'\t'}"
    read -r mode object_type object_id <<< "$metadata"
    if [[ "$object_type" != "blob" ]] || \
       [[ "$mode" != "100644" && "$mode" != "100755" ]]; then
      echo "Refusing release build: worktree Git tree has a special entry." >&2
      return 1
    fi
    path="$ROOT_DIR/$relative_path"
    if [[ ! -f "$path" || -L "$path" ]]; then
      echo "Refusing release build: tracked worktree path is not a regular file." >&2
      return 1
    fi
    if ! actual_mode="$(/usr/bin/stat -f '%Lp' "$path")"; then return 1; fi
    if [[ "$mode" == "100755" ]]; then
      if [[ "$actual_mode" != "755" ]]; then
        echo "Refusing release build: tracked executable mode drifted." >&2
        return 1
      fi
    else
      if [[ "$actual_mode" != "644" ]]; then
        echo "Refusing release build: tracked file mode drifted." >&2
        return 1
      fi
    fi
    if ! expected_sha="$(/usr/bin/git -C "$ROOT_DIR" cat-file blob "$object_id" \
        | /usr/bin/shasum -a 256 | /usr/bin/awk '{ print $1 }')" \
       || ! actual_sha="$(/usr/bin/shasum -a 256 "$path" | /usr/bin/awk '{ print $1 }')" \
       || [[ "$actual_sha" != "$expected_sha" ]]; then
      echo "Refusing release build: tracked file content drifted." >&2
      return 1
    fi
  done < "$listing"
  if [[ "$entry_count" -eq 0 ]]; then
    echo "Refusing release build: worktree Git tree is empty." >&2
    return 1
  fi
  local input_extras
  if ! input_extras="$(/usr/bin/git -C "$ROOT_DIR" \
    -c core.fsmonitor=false \
    -c core.untrackedCache=false \
    status --porcelain=v1 --untracked-files=all --ignored=matching -- \
    apps/PersonalAIMonitorMenuBar tools/generate_app_icon.swift)" \
     || [[ -n "$input_extras" ]]; then
    echo "Refusing release build: build inputs contain extra files." >&2
    return 1
  fi
}

assert_source_unchanged() {
  local current_revision current_status current_source_hash current_core_hash
  if ! current_core_hash="$(compute_core_code_hash)"; then
    echo "Refusing to package: Core provenance could not be recomputed." >&2
    return 1
  fi
  if [[ "$GIT_AVAILABLE" == "1" ]]; then
    if ! assert_git_controls; then
      return 1
    fi
    if [[ "$GIT_DIRTY" == "false" ]]; then
      if ! assert_worktree_matches_commit; then
        return 1
      fi
    fi
    if ! current_revision="$(/usr/bin/git -C "$ROOT_DIR" rev-parse --short=12 HEAD)"; then
      echo "Refusing to package: source revision could not be recomputed." >&2
      return 1
    fi
    if ! current_status="$(git_status_exact)"; then
      echo "Refusing to package: source status could not be recomputed." >&2
      return 1
    fi
    if ! current_source_hash="$(compute_source_tree_hash)"; then
      echo "Refusing to package: source provenance could not be recomputed." >&2
      return 1
    fi
    if [[ "$current_revision" != "$GIT_REVISION" || "$current_status" != "$GIT_STATUS" || "$current_source_hash" != "$SOURCE_TREE_HASH" ]]; then
      echo "Refusing to package: source changed during the build." >&2
      exit 3
    fi
  fi
  if [[ "$current_core_hash" != "$CORE_CODE_HASH" ]]; then
    echo "Refusing to package: core code changed during the build." >&2
    exit 3
  fi
}

if /usr/bin/git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_AVAILABLE=1
  if ! assert_git_controls; then
    exit 2
  fi
  if ! GIT_FULL_REVISION="$(/usr/bin/git -C "$ROOT_DIR" rev-parse HEAD)"; then
    echo "Refusing release build: full Git revision is unavailable." >&2
    exit 2
  fi
  if ! GIT_REVISION="$(/usr/bin/git -C "$ROOT_DIR" rev-parse --short=12 HEAD)"; then
    echo "Refusing release build: short Git revision is unavailable." >&2
    exit 2
  fi
  if ! GIT_STATUS="$(git_status_exact)"; then
    echo "Refusing release build: Git status is unavailable." >&2
    exit 2
  fi
  GIT_TRACKED_CHANGE_COUNT="$(printf '%s\n' "$GIT_STATUS" | /usr/bin/awk 'NF && substr($0, 1, 2) != "??" { count++ } END { print count + 0 }')"
  GIT_UNTRACKED_COUNT="$(printf '%s\n' "$GIT_STATUS" | /usr/bin/awk 'NF && substr($0, 1, 2) == "??" { count++ } END { print count + 0 }')"
  if [[ -z "$GIT_STATUS" ]]; then
    GIT_DIRTY="false"
    GIT_DIRTY_PLIST="<false/>"
    GIT_DIRTY_REASON="clean"
  else
    GIT_DIRTY="true"
    GIT_DIRTY_PLIST="<true/>"
    GIT_DIRTY_REASON="tracked=$GIT_TRACKED_CHANGE_COUNT untracked=$GIT_UNTRACKED_COUNT"
  fi
else
  GIT_AVAILABLE=0
  GIT_FULL_REVISION="unknown"
  GIT_REVISION="unknown"
  GIT_TRACKED_CHANGE_COUNT=0
  GIT_UNTRACKED_COUNT=0
  GIT_DIRTY="true"
  GIT_DIRTY_PLIST="<true/>"
  GIT_DIRTY_REASON="git_unavailable"
  SOURCE_TREE_HASH="unknown"
fi

if [[ "$GIT_DIRTY" == "true" && "$ALLOW_DIRTY_BUILD" != "1" ]]; then
  echo "Refusing to build from an uncommitted source tree ($GIT_DIRTY_REASON)." >&2
  echo "Commit the complete source snapshot first, or use --allow-dirty-development for an explicit development-only build." >&2
  exit 2
fi

if [[ "$GIT_AVAILABLE" == "1" ]]; then
  if [[ "$GIT_DIRTY" == "false" ]]; then
    if ! assert_worktree_matches_commit; then
      exit 2
    fi
  fi
  if ! SOURCE_TREE_HASH="$(compute_source_tree_hash)"; then
    echo "Refusing release build: source provenance is unavailable." >&2
    exit 2
  fi
fi

if ! CORE_CODE_HASH="$(compute_core_code_hash)"; then
  echo "Refusing release build: Core provenance is unavailable." >&2
  exit 2
fi

if [[ "$PROVENANCE_ONLY" == "1" ]]; then
  printf 'git_revision=%s\n' "$GIT_REVISION"
  printf 'git_dirty=%s\n' "$GIT_DIRTY"
  printf 'git_dirty_reason=%s\n' "$GIT_DIRTY_REASON"
  printf 'source_tree_hash=%s\n' "$SOURCE_TREE_HASH"
  printf 'core_code_hash=%s\n' "$CORE_CODE_HASH"
  exit 0
fi

/bin/mkdir -p "$HOME" "$SWIFT_CACHE_ROOT/cache" "$SWIFT_CACHE_ROOT/config" \
  "$SWIFT_CACHE_ROOT/security" "$SWIFT_CACHE_ROOT/clang-module-cache" "$SWIFT_SCRATCH_PATH"
/bin/chmod 700 "$EPHEMERAL_BUILD_ROOT" "$HOME" "$SWIFT_CACHE_ROOT" \
  "$SWIFT_CACHE_ROOT/cache" "$SWIFT_CACHE_ROOT/config" "$SWIFT_CACHE_ROOT/security" \
  "$SWIFT_CACHE_ROOT/clang-module-cache" "$SWIFT_SCRATCH_PATH"
SWIFT_BIN_PATH="$(/usr/bin/swift build "${SWIFT_BUILD_ARGS[@]}" -c release --show-bin-path)"
/usr/bin/swift build "${SWIFT_BUILD_ARGS[@]}" -c release --product PersonalAIMonitorMenuBar
EXECUTABLE="$SWIFT_BIN_PATH/PersonalAIMonitorMenuBar"
if [[ ! -x "$EXECUTABLE" ]]; then
  echo "Refusing to package: Swift executable is missing at $EXECUTABLE" >&2
  exit 3
fi

# The unchanged owner-approved artwork is a commit-bound static resource.
# Regeneration is an explicit asset change; isolated repairs never need GUI IPC.
(cd "$ROOT_DIR/assets/agi" && /usr/bin/shasum -a 256 -c AGI-app-icon.sha256)
/bin/mkdir -p "$ROOT_DIR/build"
/bin/cp "$ROOT_DIR/assets/agi/AGI-app-icon.icns" "$ICNS_FILE"
if [[ -n "${DEVELOPER_DIR-}" && "$DEVELOPER_DIR" != "/Library/Developer/CommandLineTools" ]]; then
  if ! XCODE_IDENTITY_AFTER="$(capture_xcode_identity)" \
    || [[ ! "$XCODE_IDENTITY_AFTER" =~ ^[0-9a-f]{64}$ ]] \
    || [[ "$XCODE_IDENTITY_AFTER" != "$XCODE_IDENTITY_BEFORE" ]]; then
    echo "Refusing to package: Xcode identity changed during the build." >&2
    exit 3
  fi
fi
if ! assert_source_unchanged; then
  exit 3
fi

/bin/rm -rf "$STAGING_APP_DIR"
/bin/mkdir -p "$STAGING_APP_DIR/Contents/MacOS" "$STAGING_APP_DIR/Contents/Resources"
/bin/chmod 755 "$STAGING_APP_DIR" "$STAGING_APP_DIR/Contents" \
  "$STAGING_APP_DIR/Contents/MacOS" "$STAGING_APP_DIR/Contents/Resources"
/bin/cp "$EXECUTABLE" "$STAGING_APP_DIR/Contents/MacOS/PersonalAIMonitor"
/bin/cp "$ICNS_FILE" "$STAGING_APP_DIR/Contents/Resources/AppIcon.icns"
/bin/chmod 755 "$STAGING_APP_DIR/Contents/MacOS/PersonalAIMonitor"
/bin/chmod 644 "$STAGING_APP_DIR/Contents/Resources/AppIcon.icns"

/bin/cat > "$STAGING_APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>PersonalAIMonitor</string>
  <key>CFBundleIdentifier</key>
  <string>app.personalai.monitor</string>
  <key>CFBundleName</key>
  <string>AGI</string>
  <key>CFBundleDisplayName</key>
  <string>AGI</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon.icns</string>
  <key>CFBundleShortVersionString</key>
  <string>$MARKETING_VERSION</string>
  <key>CFBundleVersion</key>
  <string>$BUILD_NUMBER</string>
  <key>PAMBuildTimestamp</key>
  <string>$BUILD_TIMESTAMP</string>
  <key>PAMGitRevision</key>
  <string>$GIT_REVISION</string>
  <key>PAMGitDirty</key>
  $GIT_DIRTY_PLIST
  <key>PAMGitDirtyReason</key>
  <string>$GIT_DIRTY_REASON</string>
  <key>PAMGitTrackedChangeCount</key>
  <integer>$GIT_TRACKED_CHANGE_COUNT</integer>
  <key>PAMGitUntrackedCount</key>
  <integer>$GIT_UNTRACKED_COUNT</integer>
  <key>PAMSourceTreeHash</key>
  <string>$SOURCE_TREE_HASH</string>
  <key>PAMCoreCodeHash</key>
  <string>$CORE_CODE_HASH</string>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>AGI captures local keyframes from your own Mac to build a private, searchable activity timeline.</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>AGI may ask browsers for the active URL to improve local context attribution.</string>
</dict>
</plist>
PLIST
/bin/chmod 644 "$STAGING_APP_DIR/Contents/Info.plist"

/usr/bin/plutil -lint "$STAGING_APP_DIR/Contents/Info.plist" >/dev/null
if ! assert_source_unchanged; then
  exit 3
fi
/usr/bin/codesign --force --deep --sign - "$STAGING_APP_DIR"
/usr/bin/codesign --verify --deep --strict "$STAGING_APP_DIR"
if ! assert_source_unchanged; then
  exit 3
fi
/bin/rm -rf "$APP_DIR"
/bin/mv "$STAGING_APP_DIR" "$APP_DIR"

echo "Built $APP_DIR"
echo "Version: $MARKETING_VERSION"
echo "Build: $BUILD_NUMBER"
echo "Source: $GIT_REVISION dirty=$GIT_DIRTY reason=$GIT_DIRTY_REASON tree=$SOURCE_TREE_HASH core=$CORE_CODE_HASH"
echo "For release builds, sign and notarize this app before distribution."
