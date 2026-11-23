#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
check_root=$(mktemp -d "${TMPDIR:-/tmp}/agi-publication-check.XXXXXX")
trap 'rm -rf "$check_root"' EXIT HUP INT TERM
cd "$project_root/apps/PersonalAIMonitorMenuBar"
/usr/bin/swiftc -parse-as-library -emit-library -emit-module -module-name PAMInteractionModel -emit-module-path "$check_root/PAMInteractionModel.swiftmodule" -o "$check_root/libPAMInteractionModel.dylib" Sources/PAMInteractionModel/PublicationDraftState.swift Sources/PAMInteractionModel/IdlePublicationPolicy.swift
/usr/bin/swiftc -parse-as-library -I "$check_root" -L "$check_root" -lPAMInteractionModel -Xlinker -rpath -Xlinker "$check_root" Sources/PersonalAIMonitorMenuBar/PublicationDrafts.swift Tests/PublicationDraftIntegrationChecks.swift -o "$check_root/drafts"
"$check_root/drafts"
/usr/bin/swiftc -parse-as-library -I "$check_root" -L "$check_root" -lPAMInteractionModel -Xlinker -rpath -Xlinker "$check_root" Sources/PersonalAIMonitorMenuBar/PublicationDrafts.swift Sources/PersonalAIMonitorMenuBar/IdlePublicationCoordinator.swift Sources/PersonalAIMonitorMenuBar/ConsoleLaunchIntent.swift Tests/PublicationExitIntegrationChecks.swift -o "$check_root/exit"
"$check_root/exit"
