from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


IDENTITY_RULE_VERSION = "app-identity-v1"

BROWSER_FAMILIES = frozenset(
    {"arc", "brave", "chrome", "dia", "edge", "firefox", "opera", "safari"}
)
DOCUMENT_EDITOR_FAMILIES = frozenset(
    {"cursor", "obsidian", "pages", "textedit", "typora", "vscode", "word", "wps"}
)
DOCUMENT_REFERENCE_FAMILIES = frozenset({"acrobat", "finder", "preview"})


_FAMILY_ALIASES: dict[str, frozenset[str]] = {
    "1password": frozenset({"1password"}),
    "acrobat": frozenset({"acrobat", "adobe acrobat", "adobe acrobat reader"}),
    "arc": frozenset({"arc"}),
    "brave": frozenset({"brave", "brave browser"}),
    "chrome": frozenset({"chrome", "google chrome"}),
    "cursor": frozenset({"cursor"}),
    "dia": frozenset({"dia"}),
    "edge": frozenset({"edge", "microsoft edge"}),
    "finder": frozenset({"finder"}),
    "firefox": frozenset({"firefox", "firefox developer edition", "firefox nightly"}),
    "knowledge": frozenset({"knowledge"}),
    "obsidian": frozenset({"obsidian"}),
    "opera": frozenset({"opera", "opera browser"}),
    "pages": frozenset({"pages"}),
    "preview": frozenset({"preview"}),
    "safari": frozenset({"safari", "safari technology preview"}),
    "textedit": frozenset({"textedit"}),
    "typora": frozenset({"typora"}),
    "vscode": frozenset({"visual studio code", "vscode"}),
    "word": frozenset({"microsoft word", "word"}),
    "wps": frozenset({"wps", "wps office", "wpsoffice"}),
}

_BUNDLE_FAMILIES: dict[str, str] = {
    "abnerworks.typora": "typora",
    "com.1password.1password": "1password",
    "com.adobe.acrobat.pro": "acrobat",
    "com.adobe.reader": "acrobat",
    "com.agilebits.onepassword7": "1password",
    "com.apple.finder": "finder",
    "com.apple.iwork.pages": "pages",
    "com.apple.preview": "preview",
    "com.apple.safari": "safari",
    "com.apple.safaritechnologypreview": "safari",
    "com.apple.textedit": "textedit",
    "com.brave.browser": "brave",
    "com.google.chrome": "chrome",
    "com.google.chrome.beta": "chrome",
    "com.google.chrome.canary": "chrome",
    "com.google.chrome.dev": "chrome",
    "com.kingsoft.wpsoffice": "wps",
    "com.kingsoft.wpsoffice.mac": "wps",
    "com.microsoft.edgemac": "edge",
    "com.microsoft.edgemac.beta": "edge",
    "com.microsoft.edgemac.canary": "edge",
    "com.microsoft.edgemac.dev": "edge",
    "com.microsoft.vscode": "vscode",
    "com.microsoft.word": "word",
    "com.operasoftware.opera": "opera",
    "com.todesktop.230313mzl4w4u92": "cursor",
    "company.thebrowser.browser": "arc",
    "company.thebrowser.dia": "dia",
    "md.obsidian": "obsidian",
    "org.mozilla.firefox": "firefox",
    "org.mozilla.firefoxdeveloperedition": "firefox",
    "org.mozilla.nightly": "firefox",
}

_ALIAS_FAMILIES: dict[str, str] = {}
for _family, _aliases in _FAMILY_ALIASES.items():
    for _alias in _aliases:
        if _alias in _ALIAS_FAMILIES and _ALIAS_FAMILIES[_alias] != _family:
            raise RuntimeError(f"ambiguous app alias: {_alias}")
        _ALIAS_FAMILIES[_alias] = _family


@dataclass(frozen=True)
class AppIdentity:
    family: str
    status: str
    source: str
    normalized_name: str
    normalized_bundle_id: str
    reason: str

    @property
    def matched(self) -> bool:
        return self.status == "matched" and bool(self.family)


def normalize_app_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[\s._-]+", " ", text).strip()


def normalize_bundle_id(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def resolve_app_identity(app_name: object = "", bundle_id: object = "") -> AppIdentity:
    """Resolve an App identity without substring or unknown-bundle fallback.

    A present bundle ID is authoritative only when it exactly matches the local
    registry.  A recognized exact name that conflicts with that bundle fails
    closed.  Exact unique name aliases are considered only when no bundle ID is
    available.
    """

    name = normalize_app_name(app_name)
    bundle = normalize_bundle_id(bundle_id)
    name_family = _ALIAS_FAMILIES.get(name, "")
    if bundle:
        bundle_family = _BUNDLE_FAMILIES.get(bundle, "")
        if not bundle_family:
            return AppIdentity("", "unknown", "bundle", name, bundle, "unknown_bundle")
        if name_family and name_family != bundle_family:
            return AppIdentity("", "conflict", "bundle", name, bundle, "bundle_name_conflict")
        return AppIdentity(bundle_family, "matched", "bundle", name, bundle, "exact_bundle")
    if name_family:
        return AppIdentity(name_family, "matched", "name", name, "", "exact_unique_name_alias")
    return AppIdentity("", "unknown", "none", name, "", "unknown_name")


def app_identity_is(app_name: object, bundle_id: object, family: str) -> bool:
    identity = resolve_app_identity(app_name, bundle_id)
    return identity.matched and identity.family == str(family or "").casefold()


def app_identity_in(app_name: object, bundle_id: object, families: Iterable[str]) -> bool:
    identity = resolve_app_identity(app_name, bundle_id)
    return identity.matched and identity.family in {str(value or "").casefold() for value in families}


def same_app_identity(
    left_name: object,
    left_bundle_id: object,
    right_name: object,
    right_bundle_id: object,
) -> bool:
    """Match two identities using exact bundles, or exact aliases if both lack bundles."""

    left_bundle = normalize_bundle_id(left_bundle_id)
    right_bundle = normalize_bundle_id(right_bundle_id)
    if left_bundle or right_bundle:
        if not left_bundle or not right_bundle or left_bundle != right_bundle:
            return False
    left = resolve_app_identity(left_name, left_bundle)
    right = resolve_app_identity(right_name, right_bundle)
    return left.matched and right.matched and left.family == right.family


def deterministic_legacy_identity_collision(app_name: object, bundle_id: object = "") -> bool:
    """Return only the three exact, demonstrated legacy substring collisions."""

    name = normalize_app_name(app_name)
    collision_target = {
        "obsidian": "dia",
        "1password": "word",
        "knowledge": "edge",
    }.get(name)
    if not collision_target:
        return False
    identity = resolve_app_identity(app_name, bundle_id)
    return not (identity.matched and identity.family == collision_target)
