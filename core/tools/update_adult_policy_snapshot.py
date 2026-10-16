#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


CORE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_ROOT))

from activity_mvp.adult_policy import ADULT_POLICY_VERSION, _fallback_policy, _hash_pair, normalize_domain


SOURCES = [
    {
        "name": "StevenBlack porn hosts",
        "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn/hosts",
        "format": "hosts",
    },
    {
        "name": "Block List Project porn",
        "url": "https://raw.githubusercontent.com/blocklistproject/Lists/master/porn.txt",
        "format": "hosts",
    },
    {
        "name": "HaGeZi NSFW",
        "url": "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/nsfw-onlydomains.txt",
        "format": "wildcard",
    },
    {
        "name": "OISD NSFW",
        "url": "https://nsfw.oisd.nl/domainswild2",
        "format": "wildcard",
    },
]
DOMAIN_CANDIDATE_RE = re.compile(r"(?i)(?:\|\|)?(?:\*\.)?([a-z0-9][a-z0-9._-]*\.[a-z]{2,})(?:\^|$|\s|/)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the local adult content policy snapshot")
    parser.add_argument("--out", default=str(CORE_ROOT / "activity_mvp" / "resources" / "adult_content_policy.json"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--bits", type=int, default=16_777_216)
    parser.add_argument("--hashes", type=int, default=11)
    parser.add_argument("--allow-insecure-tls", action="store_true")
    args = parser.parse_args()

    domains: set[str] = set()
    source_meta: list[dict] = []
    for source in SOURCES:
        raw = _fetch(source["url"], args.timeout, allow_insecure_tls=args.allow_insecure_tls)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        parsed = _parse_domains(raw.decode("utf-8", errors="ignore"))
        domains.update(parsed)
        source_meta.append(
            {
                **source,
                "fetched_at": _now(),
                "raw_bytes": len(raw),
                "raw_sha256": raw_sha256,
                "parsed_domains": len(parsed),
            }
        )

    ordered_domains = sorted(domains)
    bloom = _build_bloom(ordered_domains, args.bits, args.hashes)
    fallback = _fallback_policy()
    policy = {
        **fallback,
        "policy_version": ADULT_POLICY_VERSION,
        "generated_at": _now(),
        "sources": [
            *source_meta,
            {
                "name": "IAB Tech Lab Content Taxonomy",
                "url": "https://github.com/InteractiveAdvertisingBureau/Taxonomies",
                "format": "semantic_reference",
                "note": "Adult & Explicit Sexual Content taxonomy reference; not fetched into the domain snapshot.",
            },
            {
                "name": "Steamworks Content Survey",
                "url": "https://partner.steamgames.com/doc/store/page/content_survey",
                "format": "semantic_reference",
                "note": "Steam adult-only sexual content descriptor reference; not fetched into the domain snapshot.",
            },
        ],
        "domain_blocklist": {
            "type": "bloom_filter_sha256",
            "bits": args.bits,
            "hashes": args.hashes,
            "item_count": len(ordered_domains),
            "domain_sha256": hashlib.sha256("\n".join(ordered_domains).encode("utf-8")).hexdigest(),
            "bitset_base64": base64.b64encode(bloom).decode("ascii"),
        },
    }

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_path.chmod(0o600)
    print(
        json.dumps(
            {
                "policy_version": policy["policy_version"],
                "domain_count": len(ordered_domains),
                "domain_sha256": policy["domain_blocklist"]["domain_sha256"],
                "output": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _fetch(url: str, timeout: int, *, allow_insecure_tls: bool) -> bytes:
    request = Request(url, headers={"User-Agent": "PersonalAIMonitorPolicySnapshot/1.0"})
    context = ssl._create_unverified_context() if allow_insecure_tls else None
    with urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


def _parse_domains(text: str) -> set[str]:
    domains: set[str] = set()
    for line in text.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(("#", "!", "[", ";")):
            continue
        clean = clean.split("#", 1)[0].strip()
        clean = clean.replace("||", " ").replace("^", " ").replace("*.", " ")
        for match in DOMAIN_CANDIDATE_RE.finditer(clean):
            domain = normalize_domain(match.group(1))
            if domain:
                domains.add(domain)
    return domains


def _build_bloom(domains: list[str], bits: int, hashes: int) -> bytes:
    bitset = bytearray((bits + 7) // 8)
    for domain in domains:
        h1, h2 = _hash_pair(domain)
        for index in range(hashes):
            bit_index = (h1 + index * h2) % bits
            bitset[bit_index // 8] |= 1 << (bit_index % 8)
    return bytes(bitset)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
