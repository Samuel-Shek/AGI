from core_fixture import *


class CoreFlowTests(CoreFixture):
    def test_privacy_firewall_search_package_and_delete(self) -> None:
        report = ingest_events(self.conn, EVENTS)
        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(report["review_required"], 1)

        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertIn("Personal AI Monitor", activities[0]["activity_summary"])

        self.assertEqual(len(search(self.conn, "Personal AI Monitor")), 1)
        self.assertEqual(
            len(
                search(
                    self.conn,
                    "Personal AI Monitor",
                    start="2026-05-22T08:59:00+08:00",
                    end="2026-05-22T09:02:00+08:00",
                )
            ),
            1,
        )
        self.assertEqual(
            search(
                self.conn,
                "Personal AI Monitor",
                start="2026-05-22T09:02:00+08:00",
                end="2026-05-22T09:04:00+08:00",
            ),
            [],
        )
        self.assertEqual(len(search(self.conn, "REDACTED_TEST_CREDENTIAL_VALUE")), 0)
        self.assertEqual(len(search(self.conn, "CREDENTIAL_VALUE_DO_NOT_STORE")), 0)

        package_result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )
        package = package_result["package"]
        self.assertFalse(package["raw_access"])
        self.assertEqual(package["quarantine_status"], "clear_only")
        self.assertEqual(package["activities"], [])
        self.assertEqual(len(package["safe_timeline_events"]), 3)
        self.assertEqual(
            [item["kind"] for item in package["safe_timeline_events"]],
            ["unverified_clue", "protected", "gap"],
        )
        self.assertNotIn("ocr_text", json.dumps(package, ensure_ascii=False))
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", package_result["json_path"])
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", Path(package_result["json_path"]).read_text(encoding="utf-8"))
        self.assertIn("Safe Timeline", Path(package_result["markdown_path"]).read_text(encoding="utf-8"))

        package_id = package["package_id"]
        json_path = Path(package_result["json_path"])
        markdown_path = Path(package_result["markdown_path"])
        self.assertEqual(get_package(self.conn, package_id)["source_status"], "active")
        package_rows = list_packages(self.conn)
        self.assertEqual(len(package_rows), 1)
        self.assertNotIn("cache_ref", package_rows[0])

        deletion = delete_range(
            self.conn,
            "2026-05-22T08:59:00+08:00",
            "2026-05-22T09:02:00+08:00",
        )
        self.assertEqual(deletion["activities_deleted"], 1)
        self.assertEqual(deletion["deleted_activity_count"], 1)
        self.assertGreaterEqual(deletion["cascade"]["search_index"], 1)
        self.assertGreaterEqual(deletion["cascade"]["coverage_ledger"], 1)
        self.assertEqual(deletion["cascade"]["ha_source_records"], 1)
        self.assertEqual(deletion["cascade"]["ha_package_cache_files"], 2)
        self.assertEqual(deletion["cascade"]["gap_events_created"], 1)
        self.assertEqual(len(search(self.conn, "Personal AI Monitor")), 0)
        deletion_gap = self.conn.execute(
            "SELECT reason, detail FROM gap_events WHERE started_at = ? AND ended_at = ?",
            ("2026-05-22T08:59:00+08:00", "2026-05-22T09:02:00+08:00"),
        ).fetchone()
        self.assertIsNotNone(deletion_gap)
        self.assertEqual(deletion_gap["reason"], "user_deleted")
        self.assertIn("content-free gap", deletion_gap["detail"])
        self.assertEqual(get_package(self.conn, package_id)["source_status"], "user_deleted")
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())


    def test_ha_package_keeps_versioned_content_free_clue_without_private_body(self) -> None:
        body_marker = "GROUP_F_UNVERIFIED_BODY_MUST_STAY_LOCAL_7f2d9a"
        title_marker = "GROUP_F_PRIVATE_WINDOW_TITLE_8c41"
        event = {
            "captured_at": "2026-05-22T09:20:00+08:00",
            "display_id": "main",
            "app_name": "Obsidian",
            "bundle_id": "md.obsidian",
            "window_title": title_marker,
            "ocr_text": f"{body_marker} migration design notes",
            "context_confidence": 0.9,
        }
        report = ingest_events(self.conn, [event])
        self.assertEqual(report["clear"], 1)

        package_result = generate_package(
            self.conn,
            "2026-05-22T09:19:00+08:00",
            "2026-05-22T09:21:00+08:00",
            self.root / "packages",
        )
        package = package_result["package"]
        clues = [
            item
            for item in package["safe_timeline_events"]
            if item.get("kind") == "unverified_clue"
        ]
        self.assertEqual(len(clues), 1)
        clue = clues[0]
        self.assertEqual(
            set(clue),
            {
                "kind",
                "contract_version",
                "source_type",
                "source_ref",
                "time_range",
                "verification_state",
                "reason",
                "reason_code",
                "display_ids",
                "privacy_level",
                "dedupe_status",
                "app_class",
                "trusted",
                "consumed",
                "delivered",
                "cleaned_text_included",
                "raw_access",
            },
        )
        self.assertEqual(clue["contract_version"], "HAUnverifiedClueV1")
        self.assertEqual(clue["source_type"], "content_free_unverified_clue")
        self.assertEqual(clue["verification_state"], "unverified")
        self.assertFalse(clue["trusted"])
        self.assertFalse(clue["consumed"])
        self.assertFalse(clue["delivered"])
        self.assertFalse(clue["cleaned_text_included"])
        self.assertFalse(clue["raw_access"])
        self.assertEqual(clue["display_ids"], [])
        for forbidden_key in (
            "summary",
            "activity_summary",
            "cleaned_content",
            "cleaned_text",
            "ocr_text",
            "window_title",
            "url",
            "domain",
            "apps",
            "document_contexts",
            "evidence_refs",
        ):
            self.assertNotIn(forbidden_key, clue)

        serialized = json.dumps(package, ensure_ascii=False)
        json_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        markdown_text = Path(package_result["markdown_path"]).read_text(encoding="utf-8")
        for marker in (body_marker, title_marker):
            self.assertNotIn(marker, serialized)
            self.assertNotIn(marker, json_text)
            self.assertNotIn(marker, markdown_text)


    def test_high_risk_payment_and_otp_never_enter_clear_surfaces(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:00:00+08:00",
                "display_id": "main",
                "app_name": "Safari",
                "bundle_id": "com.apple.Safari",
                "window_title": "Stripe checkout",
                "url": "https://checkout.stripe.com/pay/test",
                "domain": "checkout.stripe.com",
                "ocr_text": "card number 4242 4242 4242 4242 cvv 123",
            },
            {
                "captured_at": "2026-05-22T10:01:00+08:00",
                "display_id": "main",
                "app_name": "Messages",
                "window_title": "验证码",
                "ocr_text": "你的验证码是 123456",
            },
            {
                "captured_at": "2026-05-22T10:02:00+08:00",
                "display_id": "main",
                "app_name": "Chrome",
                "window_title": "凭证与基础信息 - 开放平台",
                "ocr_text": "应用凭证管理页面",
            },
        ]
        with patch.dict(os.environ, {"PAM_PRIVACY_PROFILE": "personal_high_precision"}, clear=False):
            report = ingest_events(self.conn, events)
        self.assertEqual(report["blocked"], 3)
        self.assertEqual(timeline(self.conn), [])
        self.assertEqual(search(self.conn, "4242"), [])
        self.assertEqual(search(self.conn, "123456"), [])
        self.assertEqual(search(self.conn, "凭证"), [])
        package_result = generate_package(
            self.conn,
            "2026-05-22T10:00:00+08:00",
            "2026-05-22T10:05:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        self.assertNotIn("4242 4242", package_text)
        self.assertNotIn("cvv", package_text.lower())
        self.assertNotIn("123456", package_text)
        self.assertNotIn("凭证", package_text)


    def test_browser_structured_signal_blocks_private_password_context(self) -> None:
        signal = write_browser_signal(
            self.root,
            {
                "browser": "Dia",
                "bundle_id": "company.thebrowser.dia",
                "url": "https://accounts.example.com/login?token=SHOULD_NOT_STORE",
                "private_window": True,
                "has_password_field": True,
                "risk_tags": ["private_window", "password_field"],
            },
        )
        latest = read_latest_browser_signal(self.root, "Dia", "company.thebrowser.dia")
        self.assertTrue(latest)
        self.assertEqual(signal["url_redacted"], "https://accounts.example.com/[redacted]")

        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:04:00+08:00",
                    "display_id": "main",
                    "app_name": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "window_title": "Login",
                    "url": signal["url_redacted"],
                    "domain": signal["domain"],
                    "url_source": "chromium_extension_native_host",
                    "browser_signal": signal,
                    "private_window": True,
                    "browser_has_password_field": True,
                    "browser_risk_tags": signal["risk_tags"],
                    "ocr_text": "Sign in",
                }
            ],
        )
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])
        package_result = generate_package(
            self.conn,
            "2026-05-22T10:04:00+08:00",
            "2026-05-22T10:05:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        self.assertNotIn("SHOULD_NOT_STORE", package_text)


    def test_url_redaction_strips_userinfo_and_query_everywhere(self) -> None:
        raw_url = "https://user:pass@example.com/a?token=x"
        signal = sanitize_browser_signal({"url": raw_url})
        self.assertEqual(signal["domain"], "example.com")
        self.assertEqual(signal["url_redacted"], "https://example.com/[redacted]")
        self.assertEqual(safe_hostname_from_url(raw_url), "example.com")
        self.assertEqual(redact_url_for_storage(raw_url), "https://example.com/[redacted]")
        encoded = json.dumps(signal, ensure_ascii=False)
        self.assertNotIn("user:pass@", encoded)
        self.assertNotIn("token=x", encoded)
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:03:30+08:00",
                    "display_id": "main",
                    "app_name": "Chrome",
                    "window_title": "Example",
                    "url": raw_url,
                    "domain": signal["domain"],
                    "ocr_text": "Safe example account page",
                    "context_confidence": 0.9,
                }
            ],
        )
        stored_context = self.conn.execute("SELECT url_contexts FROM display_frame_contexts").fetchone()["url_contexts"]
        package_result = generate_package(
            self.conn,
            "2026-05-22T10:03:00+08:00",
            "2026-05-22T10:04:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        self.assertIn("https://example.com/[redacted]", stored_context)
        for stored_text in (stored_context, package_text):
            self.assertNotIn("user:pass@", stored_text)
            self.assertNotIn("token=x", stored_text)


    def test_browser_signals_keep_separate_short_lived_slots(self) -> None:
        chrome_signal = write_browser_signal(
            self.root,
            {
                "browser": "Chrome",
                "bundle_id": "com.google.Chrome",
                "url": "https://chrome.example/work?secret=1",
                "window_id": "10",
                "tab_id": "100",
            },
        )
        dia_signal = write_browser_signal(
            self.root,
            {
                "browser": "Dia",
                "bundle_id": "company.thebrowser.dia",
                "url": "https://dia.example/research?secret=2",
                "window_id": "20",
                "tab_id": "200",
            },
        )

        self.assertGreaterEqual(len(list_recent_browser_signals(self.root)), 2)
        self.assertEqual(
            read_latest_browser_signal(self.root, "Google Chrome", "com.google.Chrome")["domain"],
            chrome_signal["domain"],
        )
        self.assertEqual(
            read_latest_browser_signal(self.root, "Dia", "company.thebrowser.dia")["domain"],
            dia_signal["domain"],
        )


    def test_native_parent_process_identity_corrects_browser_name(self) -> None:
        self.assertEqual(
            _browser_identity_from_process_names(["/Applications/Dia.app/Contents/MacOS/Dia Helper"])["bundle_id"],
            "company.thebrowser.dia",
        )
        self.assertEqual(
            _browser_identity_from_process_names(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"])["bundle_id"],
            "com.google.Chrome",
        )
        self.assertEqual(
            _browser_identity_from_process_names(["/Applications/Arc.app/Contents/MacOS/Arc Helper"])["bundle_id"],
            "company.thebrowser.Browser",
        )


    def test_adult_browser_context_blocks_and_suppresses_context(self) -> None:
        signal = write_browser_signal(
            self.root,
            {
                "browser": "Dia",
                "bundle_id": "company.thebrowser.dia",
                "url": "https://pornhub.com/view/PRIVATE_TEST_SLUG",
            },
        )
        self.assertIn("adult_content", signal["risk_tags"])

        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:06:00+08:00",
                    "display_id": "main",
                    "app_name": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "window_title": "Pornhub PRIVATE_TEST_TITLE",
                    "url": signal["url_redacted"],
                    "domain": signal["domain"],
                    "url_source": "chromium_extension_native_host",
                    "browser_signal": signal,
                    "browser_risk_tags": signal["risk_tags"],
                    "visible_windows": [{"title": "Pornhub PRIVATE_TEST_TITLE"}],
                    "ocr_text": "Pornhub PRIVATE_TEST_OCR",
                }
            ],
        )

        self.assertEqual(report["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])
        for term in ["pornhub", "PRIVATE_TEST_TITLE", "PRIVATE_TEST_OCR"]:
            self.assertEqual(search(self.conn, term), [])

        privacy = self.conn.execute(
            "SELECT reason, app_name, domain FROM privacy_events"
        ).fetchone()
        self.assertEqual(privacy["reason"], "sensitive_content")
        self.assertEqual(privacy["app_name"], "")
        self.assertEqual(privacy["domain"], "")

        context = self.conn.execute(
            "SELECT visible_windows, app_contexts, url_contexts FROM display_frame_contexts"
        ).fetchone()
        encoded_context = json.dumps(dict(context), ensure_ascii=False).lower()
        self.assertNotIn("pornhub", encoded_context)
        self.assertNotIn("adult_content", encoded_context)
        self.assertNotIn("private_test", encoded_context)

        package_result = generate_package(
            self.conn,
            "2026-05-22T10:06:00+08:00",
            "2026-05-22T10:07:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8").lower()
        self.assertIn("sensitive_content", package_text)
        self.assertNotIn("pornhub", package_text)
        self.assertNotIn("adult_content", package_text)
        self.assertNotIn("private_test", package_text)


    def test_adult_policy_uses_public_snapshot_without_plain_domain_leakage(self) -> None:
        metadata = adult_policy_metadata()
        self.assertEqual(metadata["policy_version"], "adult-policy-0.4.0")
        self.assertGreater(metadata["domain_snapshot_count"], 100_000)
        self.assertTrue(adult_domain_hit("0-0-adult-superstore.com").matched)
        self.assertTrue(adult_domain_hit("https://sub.0-0-adult-superstore.com/watch").matched)
        self.assertFalse(adult_domain_hit("adultswim.com").matched)
        self.assertFalse(adult_text_hit("adult learning course and sex education policy").matched)


    def test_browser_signal_tags_public_snapshot_domain(self) -> None:
        signal = write_browser_signal(
            self.root,
            {
                "browser": "Chrome",
                "bundle_id": "com.google.Chrome",
                "url": "https://0-0-adult-superstore.com/watch/PRIVATE_TEST_SLUG",
            },
        )
        self.assertIn("adult_content", signal["risk_tags"])


    def test_legacy_adult_privacy_event_is_sanitized_on_export(self) -> None:
        self.conn.execute(
            """
            INSERT INTO privacy_events(id, started_at, ended_at, type, reason, app_name, domain, action, deletion_group_id)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "privacy_legacy_adult",
                "2026-05-22T10:06:00+08:00",
                "2026-05-22T10:07:00+08:00",
                "privacy_firewall_blocked",
                "adult_content",
                "Chrome",
                "pornhub.com",
                "drop_content_record_privacy_event",
                "dg_legacy_adult",
            ),
        )
        self.conn.commit()

        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T10:05:00+08:00",
            "2026-05-22T10:08:00+08:00",
        )
        self.assertEqual(export["privacy_events"][0]["reason"], "sensitive_content")
        self.assertEqual(export["privacy_events"][0]["app_name"], "")
        self.assertEqual(export["privacy_events"][0]["domain"], "")
        markdown = render_cleaned_timeline_markdown(export).lower()
        self.assertNotIn("pornhub", markdown)
        self.assertNotIn("adult_content", markdown)


    def test_steam_adult_content_blocks_without_blocking_normal_steam(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:07:00+08:00",
                "display_id": "main",
                "app_name": "Steam",
                "bundle_id": "com.valvesoftware.steam",
                "window_title": "Steam Library",
                "ocr_text": "Download updates for ordinary game",
            },
            {
                "captured_at": "2026-05-22T10:08:00+08:00",
                "display_id": "main",
                "app_name": "Steam",
                "bundle_id": "com.valvesoftware.steam",
                "window_title": "STEAM_ADULT_TEST_GAME",
                "ocr_text": "Adult only sexual content STEAM_ADULT_TEST_GAME",
            },
        ]

        with patch.dict(os.environ, {"PAM_PRIVACY_PROFILE": "personal_high_precision"}, clear=False):
            report = ingest_events(self.conn, events)
        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["blocked"], 1)
        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertIn("ordinary game", activities[0]["activity_summary"])
        self.assertEqual(search(self.conn, "STEAM_ADULT_TEST_GAME"), [])

        privacy = self.conn.execute(
            "SELECT reason, app_name, domain FROM privacy_events"
        ).fetchone()
        self.assertEqual(privacy["reason"], "sensitive_content")
        self.assertEqual(privacy["app_name"], "")
        self.assertEqual(privacy["domain"], "")


    def test_local_adult_media_context_blocks_without_filename_residue(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:09:00+08:00",
                    "display_id": "main",
                    "app_name": "QuickTime Player",
                    "bundle_id": "com.apple.QuickTimePlayerX",
                    "window_title": "PRIVATE_ADULT_VIDEO_NAME.mp4",
                    "ocr_text": "adult video PRIVATE_ADULT_VIDEO_NAME",
                }
            ],
        )

        self.assertEqual(report["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])
        self.assertEqual(search(self.conn, "PRIVATE_ADULT_VIDEO_NAME"), [])
        privacy = self.conn.execute(
            "SELECT reason, app_name, domain FROM privacy_events"
        ).fetchone()
        self.assertEqual(privacy["reason"], "sensitive_content")
        self.assertEqual(privacy["app_name"], "")


    def test_adult_policy_discussion_is_partially_redacted_not_blocked(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:10:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "bundle_id": "com.openai.codex",
                    "window_title": "隐私规则讨论",
                    "ocr_text": "讨论 Pornhub 网站应该被清洗，不是实际浏览。",
                }
            ],
        )

        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["partial_redacted"], 1)
        self.assertEqual(report["blocked"], 0)
        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertIn("局部敏感内容已清洗", activities[0]["activity_summary"])
        self.assertEqual(search(self.conn, "Pornhub"), [])


    def test_personal_medical_content_blocks_while_oura_and_pet_contexts_stay_clear(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-14T09:00:00+08:00",
                    "display_id": "main",
                    "app_name": "ChatGPT",
                    "window_title": "病情咨询",
                    "ocr_text": "我的体检报告里血常规异常，医生建议复诊并讨论用药方案。",
                    "visible_windows": [{"title": "病情咨询"}],
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-14T09:02:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "OuraRing 周报",
                    "ocr_text": "Oura readiness sleep score daily stress HRV steps workout weekly summary.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-14T09:04:00+08:00",
                    "display_id": "main",
                    "app_name": "ChatGPT",
                    "window_title": "宠物问诊",
                    "ocr_text": "我的狗狗咳嗽，想咨询宠物医院和兽医用药建议。",
                    "context_confidence": 0.9,
                },
            ],
        )

        self.assertEqual(report["blocked"], 1)
        self.assertEqual(report["clear"], 2)
        self.assertEqual(timeline(self.conn)[0]["activity_summary"].count("Oura"), 1)
        self.assertEqual(search(self.conn, "血常规"), [])
        self.assertEqual(search(self.conn, "用药方案"), [])
        self.assertEqual(len(search(self.conn, "Oura")), 1)
        self.assertEqual(len(search(self.conn, "狗狗")), 1)

        privacy = self.conn.execute(
            "SELECT reason, app_name, domain FROM privacy_events"
        ).fetchone()
        self.assertEqual(privacy["reason"], "personal_medical_content")
        self.assertEqual(privacy["app_name"], "")
        self.assertEqual(privacy["domain"], "")
        context = self.conn.execute(
            "SELECT visible_windows, app_contexts, url_contexts FROM display_frame_contexts WHERE privacy_flags LIKE '%personal_medical%'"
        ).fetchone()
        encoded_context = json.dumps(dict(context), ensure_ascii=False)
        self.assertNotIn("病情咨询", encoded_context)
        self.assertNotIn("血常规", encoded_context)


    def test_browser_native_host_uses_fixed_manifest_extension_id(self) -> None:
        public_key = (
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAs4KoerhqMF72e+RObfUfAwdieKYtLvUKynQZFOHDUDYgC2uiCVUdieITqJtOAa4F+fYQdiMBs1DW2YfTPHVDHT1TjB1LYxS+2U91cp5L/T98cvc4/UAkQycAUMxlTR4+fR6CNSbPzlXy+X6BFSpSf4NN9ZJY12AM2d0AFg1PAaoECAyge1COc9c4KzYYwtotjjUdIMCbKOtStlbG3jem/JGksqBx4AaVh3Y5CtimpmwSYAD1fqpbSDgnoM86phJFdPadpyW7ZI+dL5HmOfUA3RYJCkDwUBJsyLvhQ1qUzIr6NPXRT35kl1C7m8dp+ZCdZcGgTT28UScN/mNUipSt7wIDAQAB"
        )
        self.assertEqual(extension_id_from_public_key(public_key), "neinjkcikcfdbafjffgadppbpafpjnbm")

        core_root = self.root / "core"
        (core_root / "browser_extension").mkdir(parents=True)
        (core_root / "browser_extension" / "manifest.json").write_text(
            json.dumps({"key": public_key}),
            encoding="utf-8",
        )
        (core_root / "tools").mkdir()
        host_script = core_root / "tools" / "browser_native_host.py"
        host_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        with patch.dict(CHROMIUM_HOST_DIRS, {"chrome": self.root / "ChromeHosts"}, clear=True):
            result = install_native_host_manifests(core_root, ["chrome"])

        self.assertTrue(result["chrome"]["origin_configured"])
        self.assertEqual(result["chrome"]["extension_id"], "neinjkcikcfdbafjffgadppbpafpjnbm")
        manifest_text = Path(result["chrome"]["manifest"]).read_text(encoding="utf-8")
        self.assertIn("chrome-extension://neinjkcikcfdbafjffgadppbpafpjnbm/", manifest_text)


    def test_browser_extension_manifest_and_content_permissions_are_locked(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        manifest = json.loads((repo_root / "core/browser_extension/manifest.json").read_text(encoding="utf-8"))
        permissions = set(manifest.get("permissions") or [])
        forbidden = {"history", "bookmarks", "clipboardRead", "clipboardWrite", "downloads", "cookies"}

        self.assertFalse(permissions & forbidden)
        self.assertEqual(permissions, {"nativeMessaging", "tabs", "webNavigation"})
        self.assertEqual(set(manifest.get("host_permissions") or []), {"<all_urls>"})
        self.assertEqual(set(manifest["content_scripts"][0]["matches"]), {"<all_urls>"})
        self.assertIn("policy.js", manifest["content_scripts"][0]["js"])
        content = (repo_root / "core/browser_extension/content.js").read_text(encoding="utf-8")
        self.assertNotRegex(content, r"\.value\b")
        self.assertNotIn("getSelection(", content)
        self.assertNotIn("document.body.innerText", content)
        self.assertNotIn("document.documentElement.innerText", content)


    def test_sck_helper_writes_raw_keyframes_with_private_permissions(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        source = (repo_root / "core/tools/sck_keyframe.swift").read_text(encoding="utf-8")

        self.assertIn("umask(0o077)", source)
        self.assertIn(".posixPermissions: 0o600", source)
        self.assertIn(".posixPermissions: 0o700", source)


    def test_backup_hygiene_reports_old_sqlite_without_deleting(self) -> None:
        fake_home = self.root / "home"
        backup_db = fake_home / "PersonalAI-Monitor-backups" / "old-deploy" / "core" / "data" / "activity.sqlite"
        backup_db.parent.mkdir(parents=True)
        backup_db.write_bytes(b"sqlite backup")
        backup_db.chmod(0o644)
        live_db = fake_home / "Library/Application Support/Personal AI Monitor/core/data/activity.sqlite"
        live_db.parent.mkdir(parents=True)
        live_db.write_bytes(b"live sqlite")
        live_db.chmod(0o600)

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            report = backup_hygiene_report(self.root / "repo-core", self.root / "repo-core/data/activity.sqlite")
            full_report = data_hygiene_report(self.root / "repo-core", self.root / "repo-core/data/activity.sqlite")

        self.assertEqual(report["mode"], "read_only")
        self.assertFalse(report["raw_access"])
        self.assertFalse(full_report["raw_access"])
        self.assertTrue(backup_db.exists())
        candidates = report["backup_candidates"]
        old_candidate = next(item for item in candidates if item["path"] == str(backup_db))
        self.assertEqual(old_candidate["type"], "sqlite_database")
        self.assertTrue(old_candidate["outside_live_runtime"])
        self.assertEqual(old_candidate["risk_level"], "high")
        self.assertIn("永久删除候选", old_candidate["recommendation"])
        self.assertEqual(report["risk_summary"]["high_count"], 1)
        self.assertTrue(any(item["action"] == "automation_policy_note" for item in report["recommended_actions"]))
        self.assertIn("risk_summary", full_report)
        live_candidates = [item for item in candidates if item["path"] == str(live_db)]
        self.assertEqual(live_candidates, [])
        backup_location = next(item for item in full_report["locations"] if item["label"] == "personal_ai_monitor_backups")
        self.assertEqual(backup_location["recursive_marker_count"], 1)


    def test_backup_hygiene_reports_release_backups_metadata_without_reading_contents(self) -> None:
        fake_home = self.root / "home"
        live_core = fake_home / "Library/Application Support/Personal AI Monitor/core"
        release_root = live_core.parent / "release-backups"
        first_backup = release_root / "v0.5.0-app"
        second_backup = release_root / "v0.5.0-core"
        first_backup.mkdir(parents=True)
        second_backup.mkdir()
        first_payload = first_backup / "Personal AI Monitor.app"
        second_payload = second_backup / "activity.sqlite"
        first_payload.write_bytes(b"synthetic-app")
        second_payload.write_bytes(b"synthetic-database")
        link = release_root / "latest"
        link.symlink_to(first_backup.name)

        with patch.object(Path, "read_bytes", side_effect=AssertionError("backup contents must not be read")), patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("backup contents must not be read"),
        ):
            inventory = _release_backups_inventory(release_root)

        self.assertEqual(inventory["mode"], "read_only")
        self.assertFalse(inventory["raw_access"])
        self.assertTrue(inventory["exists"])
        self.assertTrue(inventory["is_directory"])
        self.assertTrue(inventory["scan_complete"])
        self.assertEqual(inventory["scan_error_count"], 0)
        self.assertEqual(inventory["entry_count"], 3)
        self.assertEqual([item["name"] for item in inventory["items"]], ["latest", "v0.5.0-app", "v0.5.0-core"])
        self.assertEqual(inventory["logical_bytes"], sum(item["logical_bytes"] for item in inventory["items"]))
        self.assertEqual(inventory["allocated_bytes"], sum(item["allocated_bytes"] for item in inventory["items"]))
        self.assertTrue(inventory["apfs_clone_uncertainty"])
        self.assertTrue(all(item["apfs_clone_uncertainty"] for item in inventory["items"]))
        self.assertFalse(inventory["automatic_cleanup_enabled"])
        self.assertEqual(first_payload.read_bytes(), b"synthetic-app")
        self.assertEqual(second_payload.read_bytes(), b"synthetic-database")
        self.assertTrue(link.is_symlink())

        redirected_root = self.root / "release-backups-link"
        redirected_root.symlink_to(release_root, target_is_directory=True)
        redirected_inventory = _release_backups_inventory(redirected_root)
        self.assertEqual(redirected_inventory["root_kind"], "symlink")
        self.assertFalse(redirected_inventory["is_directory"])
        self.assertFalse(redirected_inventory["scan_complete"])
        self.assertEqual(redirected_inventory["items"], [])

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            report = backup_hygiene_report(live_core, live_core / "data/activity.sqlite")

        self.assertFalse(report["raw_access"])
        self.assertEqual(report["release_backups"], inventory)


    def test_backup_hygiene_reports_invalidated_ha_cache_residue_without_deleting(self) -> None:
        fake_home = self.root / "home"
        live_core = fake_home / "Library/Application Support/Personal AI Monitor/core"
        live_db = live_core / "data/activity.sqlite"
        live_db.parent.mkdir(parents=True, exist_ok=True)
        live_conn = ensure_ready(live_db)
        try:
            ingest_events(live_conn, EVENTS)
            package_result = generate_package(
                live_conn,
                "2026-05-22T09:00:00+08:00",
                "2026-05-22T09:12:00+08:00",
                live_core / "data/ha_packages",
            )
            package_id = package_result["package"]["package_id"]
            json_path = Path(package_result["json_path"])
            markdown_path = Path(package_result["markdown_path"])
            revoke_package(live_conn, package_id, "test_revoked")
            json_path.write_text("{}", encoding="utf-8")
            markdown_path.write_text("# stale package", encoding="utf-8")
        finally:
            live_conn.close()

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            report = backup_hygiene_report(live_core, live_db)

        self.assertEqual(report["mode"], "read_only")
        self.assertTrue(json_path.exists())
        self.assertTrue(markdown_path.exists())
        residues = [item for item in report["ha_cache_residue_candidates"] if item["package_id"] == package_id]
        self.assertGreaterEqual(len(residues), 2)
        self.assertTrue(all(item["type"] == "ha_package_cache_residue" for item in residues))
        self.assertTrue(all(item["record_status"] == "revoked" for item in residues))
        self.assertTrue(all("db_status_revoked" in item["residue_reasons"] for item in residues))
        self.assertEqual(report["ha_cache_residue_summary"]["by_record_status"]["revoked"], len(residues))
        self.assertTrue(any(item["type"] == "ha_package_cache_residue" for item in report["backup_candidates"]))

        dry_run = safe_delete_ha_cache_residue([json_path, markdown_path], live_core / "data/ha_packages")
        self.assertEqual(dry_run["mode"], "dry_run")
        self.assertEqual(dry_run["deleted_count"], 0)
        self.assertTrue(json_path.exists())
        self.assertTrue(markdown_path.exists())


    def test_deletion_group_exclusion_survives_sqlite_variable_cap(self) -> None:
        """Retained history must not overflow SQLite's variable limit."""
        from activity_mvp import lifecycle

        rows = [
            ("keep-1", "group-keep", "2026-08-26T00:00:00+00:00"),
            ("drop-1", "group-held", "2026-08-26T00:00:01+00:00"),
            ("keep-2", "group-other", "2026-08-26T00:00:02+00:00"),
        ]
        for signal_id, group_id, captured_at in rows:
            self.conn.execute(
                "INSERT INTO frame_signals(id,session_id,captured_at,deletion_group_id) "
                "VALUES(?,?,?,?)",
                (signal_id, "session-cap", captured_at, group_id),
            )
        self.conn.commit()

        ids = [signal_id for signal_id, _, _ in rows]
        ids.extend(f"absent-{index}" for index in range(20000))
        held = {f"group-absent-{index}" for index in range(20000)}
        held.add("group-held")

        kept = lifecycle._exclude_ids_in_deletion_groups(
            self.conn, "frame_signals", ids, held
        )

        self.assertEqual(sorted(kept), ["keep-1", "keep-2"])


    def test_hygiene_cleanup_applies_only_invalid_live_ha_cache_residue(self) -> None:
        fake_home = self.root / "home"
        live_core = fake_home / "Library/Application Support/Personal AI Monitor/core"
        live_db = live_core / "data/activity.sqlite"
        live_conn = ensure_ready(live_db)
        try:
            ingest_events(live_conn, EVENTS)
            active_result = generate_package(
                live_conn,
                "2026-05-22T09:00:00+08:00",
                "2026-05-22T09:12:00+08:00",
                live_core / "data/ha_packages",
            )
            stale_result = generate_package(
                live_conn,
                "2026-05-22T09:00:00+08:00",
                "2026-05-22T09:12:00+08:00",
                live_core / "data/ha_packages",
            )
            active_json = Path(active_result["json_path"])
            active_md = Path(active_result["markdown_path"])
            stale_json = Path(stale_result["json_path"])
            stale_md = Path(stale_result["markdown_path"])
            revoke_package(live_conn, stale_result["package"]["package_id"], "test_revoked")
            stale_json.write_text("{}", encoding="utf-8")
            stale_md.write_text("# stale package", encoding="utf-8")
        finally:
            live_conn.close()

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            dry_run = hygiene_cleanup_report(live_core, live_db)
            applied = hygiene_cleanup_report(
                live_core,
                live_db,
                apply=True,
                confirm=HYGIENE_CLEANUP_CONFIRMATION,
            )

        self.assertEqual(dry_run["mode"], "dry_run")
        self.assertGreaterEqual(dry_run["ha_cache"]["candidate_count"], 2)
        self.assertTrue(active_json.exists())
        self.assertTrue(active_md.exists())
        self.assertFalse(stale_json.exists())
        self.assertFalse(stale_md.exists())
        self.assertEqual(applied["ha_cache"]["deleted_count"], 2)


    def test_hygiene_cleanup_requires_confirmation_for_apply(self) -> None:
        with self.assertRaises(ValueError):
            hygiene_cleanup_report(self.root, self.root / "activity.sqlite", apply=True)


    def test_hygiene_cleanup_permanently_deletes_old_activity_data_snapshots(self) -> None:
        fake_home = self.root / "home"
        backup_root = fake_home / "PersonalAI-Monitor-backups"
        base_time = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        snapshots = [
            (backup_root / "snapshot-old", base_time - timedelta(days=9)),
            (backup_root / "snapshot-yesterday", base_time - timedelta(hours=12)),
        ]
        for path, modified_at in snapshots:
            (path / "core/data").mkdir(parents=True)
            (path / "core/data/activity.sqlite").write_bytes(b"sqlite backup")
            timestamp = modified_at.timestamp()
            os.utime(path, (timestamp, timestamp))
            os.utime(path / "core/data/activity.sqlite", (timestamp, timestamp))

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            result = hygiene_cleanup_report(
                self.root,
                self.root / "activity.sqlite",
                apply=True,
                confirm=HYGIENE_CLEANUP_CONFIRMATION,
                now=base_time,
            )

        self.assertFalse((backup_root / "snapshot-old").exists())
        self.assertTrue((backup_root / "snapshot-yesterday").exists())
        self.assertEqual(result["backup_rotation"]["deleted_count"], 1)
        self.assertEqual(result["backup_rotation"]["quarantined_count"], 0)
        self.assertTrue(result["backup_rotation"]["audit_path"])
        audit_text = Path(result["backup_rotation"]["audit_path"]).read_text(encoding="utf-8")
        self.assertIn("snapshot-old", audit_text)
        self.assertIn("old_activity_data_snapshot_outside_today_yesterday", audit_text)


    def test_data_hygiene_reports_old_activity_export_cache_without_deleting(self) -> None:
        fake_home = self.root / "home"
        live_core = fake_home / "Library/Application Support/Personal AI Monitor/core"
        live_db = live_core / "data/activity.sqlite"
        live_conn = ensure_ready(live_db)
        live_conn.close()
        exports = live_core / "data/exports"
        exports.mkdir(parents=True)
        stale_cleaned = exports / "live-check-cleaned.md"
        stale_learning = exports / "live-check-learning.md"
        fresh_cleaned = exports / "today-cleaned.md"
        unrelated = exports / "manual-notes.md"
        for path in [stale_cleaned, stale_learning, fresh_cleaned, unrelated]:
            path.write_text("# export", encoding="utf-8")
            path.chmod(0o600)
        base_time = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        old_time = (base_time - timedelta(days=9)).timestamp()
        for path in [stale_cleaned, stale_learning]:
            os.utime(path, (old_time, old_time))
        fresh_time = (base_time - timedelta(hours=2)).timestamp()
        os.utime(fresh_cleaned, (fresh_time, fresh_time))
        os.utime(unrelated, (old_time, old_time))

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            report = data_hygiene_report(live_core, live_db)
            dry_run = hygiene_cleanup_report(
                live_core,
                live_db,
                include_ha_cache=False,
                include_backup_rotation=False,
                include_runtime_permissions=False,
                now=base_time,
            )

        self.assertTrue(stale_cleaned.exists())
        self.assertTrue(stale_learning.exists())
        self.assertFalse(report["activity_export_cache_auto_cleanup_enabled"])
        self.assertFalse(dry_run["activity_export_cache_auto_cleanup_enabled"])
        self.assertFalse(dry_run["policy"]["activity_export_cache_auto_cleanup_enabled"])
        self.assertTrue(dry_run["policy"]["activity_export_cache_manual_apply_required"])
        self.assertFalse(dry_run["activity_export_cache"]["auto_cleanup_enabled"])
        self.assertTrue(dry_run["activity_export_cache"]["manual_apply_required"])
        stale_paths = {item["path"] for item in report["activity_export_cache_candidates"] if item["outside_activity_retention_window"]}
        self.assertIn(str(stale_cleaned), stale_paths)
        self.assertIn(str(stale_learning), stale_paths)
        self.assertNotIn(str(unrelated), {item["path"] for item in report["activity_export_cache_candidates"]})
        self.assertEqual(dry_run["activity_export_cache"]["candidate_count"], 2)
        self.assertEqual(dry_run["activity_export_cache"]["deleted_count"], 0)

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            applied = hygiene_cleanup_report(
                live_core,
                live_db,
                apply=True,
                confirm=HYGIENE_CLEANUP_CONFIRMATION,
                include_ha_cache=False,
                include_backup_rotation=False,
                include_runtime_permissions=False,
                now=base_time,
            )

        self.assertEqual(applied["activity_export_cache"]["deleted_count"], 2)
        self.assertFalse(applied["activity_export_cache_auto_cleanup_enabled"])
        self.assertFalse(applied["activity_export_cache"]["auto_cleanup_enabled"])
        self.assertTrue(applied["activity_export_cache"]["manual_apply_required"])
        self.assertFalse(stale_cleaned.exists())
        self.assertFalse(stale_learning.exists())
        self.assertTrue(fresh_cleaned.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(applied["activity_export_cache"]["audit_path"])
        audit_text = Path(applied["activity_export_cache"]["audit_path"]).read_text(encoding="utf-8")
        self.assertIn("live-check-cleaned.md", audit_text)
        self.assertIn("old_activity_export_cache_outside_today_yesterday", audit_text)


    def test_hygiene_cleanup_normalizes_runtime_permissions_and_preserves_bin_exec(self) -> None:
        fake_home = self.root / "home"
        live_core = fake_home / "Library/Application Support/Personal AI Monitor/core"
        live_db = live_core / "data/activity.sqlite"
        live_conn = ensure_ready(live_db)
        live_conn.close()
        data_root = live_core / "data"
        exports = data_root / "exports"
        logs = data_root / "logs"
        bin_dir = data_root / "bin"
        unrelated_dir = data_root / "unrelated-cache"
        exports.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        unrelated_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs / "server.log"
        helper = bin_dir / "pam-helper"
        unrelated_file = unrelated_dir / "notes.txt"
        log_file.write_text("log", encoding="utf-8")
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        unrelated_file.write_text("not runtime", encoding="utf-8")
        exports.chmod(0o755)
        logs.chmod(0o755)
        unrelated_dir.chmod(0o755)
        log_file.chmod(0o644)
        helper.chmod(0o755)
        unrelated_file.chmod(0o644)

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            dry_run = hygiene_cleanup_report(
                live_core,
                live_db,
                include_ha_cache=False,
                include_backup_rotation=False,
                include_activity_export_cache=False,
            )
            applied = hygiene_cleanup_report(
                live_core,
                live_db,
                apply=True,
                confirm=HYGIENE_CLEANUP_CONFIRMATION,
                include_ha_cache=False,
                include_backup_rotation=False,
                include_activity_export_cache=False,
            )

        self.assertGreaterEqual(dry_run["runtime_permissions"]["candidate_count"], 4)
        self.assertGreaterEqual(applied["runtime_permissions"]["fixed_count"], 4)
        dry_paths = {item["path"] for item in dry_run["runtime_permissions"]["actions"]}
        self.assertNotIn(str(unrelated_dir), dry_paths)
        self.assertNotIn(str(unrelated_file), dry_paths)
        self.assertEqual(oct(exports.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(logs.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(log_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(helper.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(unrelated_dir.stat().st_mode & 0o777), "0o755")
        self.assertEqual(oct(unrelated_file.stat().st_mode & 0o777), "0o644")


    def test_hygiene_cleanup_keeps_non_activity_backups_even_when_old(self) -> None:
        fake_home = self.root / "home"
        backup_root = fake_home / "PersonalAI-Monitor-backups"
        base_time = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        for index in range(2):
            path = backup_root / f"snapshot-{index}"
            path.mkdir(parents=True)
            (path / "README.md").write_text("browser extension source backup", encoding="utf-8")
            timestamp = (base_time - timedelta(days=30 + index)).timestamp()
            os.utime(path, (timestamp, timestamp))

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            result = hygiene_cleanup_report(
                self.root,
                self.root / "activity.sqlite",
                apply=True,
                confirm=HYGIENE_CLEANUP_CONFIRMATION,
                now=base_time,
            )

        self.assertTrue((backup_root / "snapshot-0").exists())
        self.assertTrue((backup_root / "snapshot-1").exists())
        self.assertEqual(result["backup_rotation"]["deleted_count"], 0)
        self.assertEqual(result["backup_rotation"]["quarantined_count"], 0)


    def test_hygiene_cleanup_purges_quarantine_only_after_grace_period(self) -> None:
        fake_home = self.root / "home"
        quarantine_root = fake_home / "Library/Application Support/Personal AI Monitor/quarantine"
        now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        expired = quarantine_root / "expired"
        fresh = quarantine_root / "fresh"
        for item_dir, delete_after in [
            (expired, now - timedelta(days=1)),
            (fresh, now + timedelta(days=1)),
        ]:
            (item_dir / "payload").mkdir(parents=True)
            (item_dir / "payload/data.sqlite").write_bytes(b"sqlite backup")
            (item_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "item_id": item_dir.name,
                        "original_path": f"/tmp/{item_dir.name}",
                        "type": "live_sqlite_backup",
                        "delete_after": delete_after.isoformat(timespec="seconds"),
                        "size_bytes": 13,
                    }
                ),
                encoding="utf-8",
            )

        with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
            result = hygiene_cleanup_report(
                self.root,
                self.root / "activity.sqlite",
                apply=True,
                confirm=HYGIENE_CLEANUP_CONFIRMATION,
                now=now,
            )

        self.assertFalse(expired.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(result["quarantine_purge"]["purged_count"], 1)


    def test_accessibility_text_can_replace_ocr_for_clear_activity(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:06:00+08:00",
                    "display_id": "main",
                    "app_name": "Notes",
                    "bundle_id": "com.apple.Notes",
                    "window_title": "Planning",
                    "ocr_text": "",
                    "accessibility_text": "Personal AI Monitor structured signals implementation notes",
                    "context_confidence": 0.9,
                }
            ],
        )
        self.assertEqual(report["clear"], 1)
        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertIn("structured signals", activities[0]["activity_summary"])
        self.assertEqual(len(search(self.conn, "structured signals")), 1)


    def test_document_signal_is_safe_in_timeline_package_and_delete(self) -> None:
        doc_path = self.root / "front-document.md"
        doc_path.write_text(
            "Personal AI Monitor structured document extraction plan.\n\n"
            + ("safe implementation detail " * 200)
            + "\n\nTRAILING_FULL_TEXT_SHOULD_NOT_APPEAR",
            encoding="utf-8",
        )
        event = {
            "captured_at": "2026-05-22T10:07:00+08:00",
            "display_id": "main",
            "app_name": "Obsidian",
            "bundle_id": "md.obsidian",
            "window_title": "front-document.md",
            "ocr_text": "Personal AI Monitor structured document extraction plan.",
            "document_path": str(doc_path),
            "context_confidence": 0.9,
        }
        document_signal = build_document_signal(self.root, event)
        self.assertIsNotNone(document_signal)
        event["document_signal"] = document_signal
        report = ingest_events(self.conn, [event])
        self.assertEqual(report["clear"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS count FROM document_signals").fetchone()["count"], 1)
        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertEqual(len(activities[0]["document_contexts"]), 1)
        self.assertNotIn(str(doc_path), json.dumps(activities, ensure_ascii=False))

        package_result = generate_package(
            self.conn,
            "2026-05-22T10:07:00+08:00",
            "2026-05-22T10:08:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        self.assertNotIn("document_contexts", package_text)
        self.assertIn("unverified_clue", package_text)
        self.assertNotIn(str(doc_path), package_text)
        self.assertNotIn("TRAILING_FULL_TEXT_SHOULD_NOT_APPEAR", package_text)

        deleted = delete_range(
            self.conn,
            "2026-05-22T10:07:00+08:00",
            "2026-05-22T10:08:00+08:00",
        )
        self.assertEqual(deleted["cascade"]["document_signals"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS count FROM document_signals").fetchone()["count"], 0)


    def test_metadata_only_document_signal_stays_out_of_document_context(self) -> None:
        pdf_path = self.root / "front-document.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nmetadata only\n")
        event = {
            "captured_at": "2026-05-22T10:09:00+08:00",
            "display_id": "main",
            "app_name": "Preview",
            "bundle_id": "com.apple.Preview",
            "window_title": "front-document.pdf",
            "ocr_text": "Visible safe PDF text from the current screen",
            "document_path": str(pdf_path),
            "context_confidence": 0.9,
        }
        document_signal = build_document_signal(self.root, event)
        self.assertIsNotNone(document_signal)
        self.assertFalse(document_signal["semantic_match"])
        self.assertEqual(document_signal["match_status"], "metadata_only")
        self.assertEqual(document_signal["context_text_for_privacy"], "")
        event["document_signal"] = document_signal

        report = ingest_events(self.conn, [event])
        self.assertEqual(report["clear"], 1)
        row = self.conn.execute(
            "SELECT semantic_match, match_status FROM document_signals"
        ).fetchone()
        self.assertEqual(row["semantic_match"], 0)
        self.assertEqual(row["match_status"], "metadata_only")
        activities = timeline(self.conn)
        self.assertEqual(activities[0]["document_contexts"], [])

        package_result = generate_package(
            self.conn,
            "2026-05-22T10:09:00+08:00",
            "2026-05-22T10:10:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        self.assertNotIn("front-document.pdf", package_text)
        self.assertNotIn("metadata_only", package_text)


    def test_pdf_self_resume_extracts_safe_summary_for_memory_candidate(self) -> None:
        pdf_path = self.root / "Alex_Resume.pdf"
        resume_text = (
            "Alex Local Owner Resume CEO Office Overseas Business Project Manager GTM B2B B2G "
            "market research contract negotiation channel operations SOP knowledge base AI tools. "
            + ("delivery leadership portfolio operations " * 40)
            + "Contact alex@example.com +86 138 0013 8000"
        )
        pdf_path.write_bytes(("%PDF-1.4\n" + resume_text + "\n%%EOF\n").encode("utf-8"))
        event = {
            "captured_at": "2026-06-10T10:11:00+08:00",
            "display_id": "main",
            "app_name": "Preview",
            "bundle_id": "com.apple.Preview",
            "window_title": "Alex_Resume.pdf",
            "ocr_text": "Alex Local Owner Resume CEO Office Overseas Business Project Manager",
            "document_path": str(pdf_path),
            "context_confidence": 0.9,
        }
        document_signal = build_document_signal(self.root, event)
        self.assertIsNotNone(document_signal)
        self.assertTrue(document_signal["semantic_match"])
        self.assertEqual(document_signal["document_parse_status"], "text_extracted")
        self.assertIn(document_signal["document_parse_method"], {"mdls_kMDItemTextContent", "pdf_printable_strings"})
        self.assertEqual(document_signal["document_source_category"], "self_resume")
        self.assertEqual(document_signal["document_memory_policy"], "self_resume_safe_summary")
        self.assertIn("角色/能力线索", document_signal["change_summary_redacted"])
        self.assertIn("GTM", document_signal["change_summary_redacted"])
        self.assertNotIn("alex@example.com", document_signal["change_summary_redacted"])
        event["document_signal"] = document_signal
        ingest_events(self.conn, [event])
        package_result = generate_package(
            self.conn,
            "2026-06-10T10:10:00+08:00",
            "2026-06-10T10:12:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, package_result["package"]["package_id"], max_items=40)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertNotIn("alex@example.com", encoded)
        self.assertNotIn("138 0013 8000", encoded)
        self.assertNotIn(str(pdf_path), encoded)


    def test_jd_document_is_reference_only_and_not_hermes_write_input(self) -> None:
        # "cv" appears only inside an unrelated path component. It must not
        # turn reference-only job material into an owner-resume signal.
        jd_parent = self.root / "archivecvnoise"
        jd_parent.mkdir(parents=True, exist_ok=True)
        jd_path = jd_parent / "target-role-jd.md"
        jd_path.write_text(
            "招聘公司 JD 岗位职责 任职要求：负责 EMEA/APAC 区域渠道打法、海外 GTM、销售策略和客户拓展。"
            "这是目标岗位要求，不是 Local Owner 的个人长期定位。",
            encoding="utf-8",
        )
        event = {
            "captured_at": "2026-06-10T10:21:00+08:00",
            "display_id": "main",
            "app_name": "Typora",
            "window_title": "target-role-jd.md",
            "ocr_text": "招聘公司 JD 岗位职责 任职要求 EMEA APAC 区域渠道打法",
            "document_path": str(jd_path),
            "context_confidence": 0.9,
        }
        document_signal = build_document_signal(self.root, event)
        self.assertIsNotNone(document_signal)
        self.assertEqual(document_signal["document_source_category"], "target_role_jd")
        self.assertEqual(document_signal["document_source_scope"], "reference_only")
        self.assertEqual(document_signal["document_memory_policy"], "reference_only")
        self.assertIn("reference_only_job_description", document_signal["document_risk_flags"])
        event["document_signal"] = document_signal
        ingest_events(self.conn, [event])
        self._seed_historical_confirmed_business_contexts(
            "2026-06-10T10:20:00+08:00",
            "2026-06-10T10:22:00+08:00",
            workstream="目标岗位研究",
            outcome="progressing",
        )
        package_result = generate_package(
            self.conn,
            "2026-06-10T10:20:00+08:00",
            "2026-06-10T10:22:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, package_result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertEqual(summary["memory_candidate_skip_reason"], "reference_only_job_document")
        self.assertGreaterEqual(summary["candidate_quality"]["target_role_jd_document_count"], 1)
        self.assertEqual(summary["review_only_candidates"], [])
        self.assertGreaterEqual(summary["ephemeral_candidate_count"], 1)
        self.assertEqual(summary["write_input_count"], 0)
        self.assertIn("reference_only_job_document", summary["write_guard_summary"])


    def test_third_party_surname_containing_owner_token_is_not_self_resume(self) -> None:
        resume_path = self.root / "John-Alexson-Curriculum-Vitae.md"
        resume_path.write_text(
            "John Alexson curriculum vitae: product leadership and channel operations.",
            encoding="utf-8",
        )
        signal = build_document_signal(
            self.root,
            {
                "captured_at": "2026-06-14T09:20:00+08:00",
                "display_id": "main",
                "app_name": "Typora",
                "window_title": resume_path.name,
                "ocr_text": "John Alexson curriculum vitae product leadership",
                "document_path": str(resume_path),
                "context_confidence": 0.9,
            },
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["document_source_category"], "ambiguous_document")
        self.assertEqual(signal["document_subject_scope"], "ambiguous_subject")
        self.assertEqual(signal["document_memory_policy"], "reference_only")
        self.assertNotEqual(signal["document_source_category"], "self_resume")


    def test_embedded_cv_and_jd_substrings_do_not_pollute_topic_tags(self) -> None:
        document_path = self.root / "OpenCV-JDK-migration.md"
        document_path.write_text(
            "OpenCV and JDK migration plan for Python3 and SwiftUI build compatibility.",
            encoding="utf-8",
        )
        signal = build_document_signal(
            self.root,
            {
                "captured_at": "2026-06-14T09:25:00+08:00",
                "display_id": "main",
                "app_name": "Typora",
                "window_title": document_path.name,
                "ocr_text": "OpenCV JDK migration Python3 SwiftUI build compatibility",
                "document_path": str(document_path),
                "context_confidence": 0.9,
            },
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["document_source_category"], "general_document")
        self.assertIn("code", signal["topic_tags"])
        self.assertNotIn("resume", signal["topic_tags"])
        self.assertNotIn("job_description", signal["topic_tags"])


    def test_personal_medical_document_is_protected_and_not_memory_input(self) -> None:
        medical_path = self.root / "2026-体检报告.md"
        medical_path.write_text(
            "我的体检报告：血常规异常，肝功能复查，医生诊断后建议复诊并讨论用药。",
            encoding="utf-8",
        )
        event = {
            "captured_at": "2026-06-14T09:30:00+08:00",
            "display_id": "main",
            "app_name": "Typora",
            "window_title": "2026-体检报告.md",
            "ocr_text": "我的体检报告 血常规 肝功能 复诊 用药",
            "document_path": str(medical_path),
            "context_confidence": 0.9,
        }
        document_signal = build_document_signal(self.root, event)
        self.assertIsNotNone(document_signal)
        encoded_signal = json.dumps(document_signal, ensure_ascii=False)
        self.assertEqual(document_signal["document_source_category"], "personal_medical_document")
        self.assertEqual(document_signal["document_source_scope"], "protected")
        self.assertEqual(document_signal["document_memory_policy"], "blocked_personal_medical")
        self.assertIn("personal_medical_document", document_signal["document_risk_flags"])
        self.assertEqual(document_signal["file_name_redacted"], "protected_medical_document.md")
        self.assertEqual(document_signal["context_text_for_privacy"], "")
        self.assertNotIn("血常规", encoded_signal)
        self.assertNotIn("肝功能", encoded_signal)
        self.assertNotIn("体检报告", encoded_signal)

        event["document_signal"] = document_signal
        report = ingest_events(self.conn, [event])
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])
        self.assertEqual(search(self.conn, "血常规"), [])
        package_result = generate_package(
            self.conn,
            "2026-06-14T09:29:00+08:00",
            "2026-06-14T09:31:00+08:00",
            self.root / "packages",
        )
        memory_summary = get_package_memory_candidates(self.conn, package_result["package"]["package_id"], max_items=40)
        self.assertEqual(memory_summary["memory_candidates"], [])
        self.assertEqual(memory_summary["memory_candidate_count"], 0)


    def test_personal_high_precision_finds_safe_document_candidate_by_window_title(self) -> None:
        docs = self.root / "docs"
        docs.mkdir()
        doc_path = docs / "candidate-note.md"
        doc_path.write_text(
            "Personal high precision document candidate visible semantic text for matching.",
            encoding="utf-8",
        )
        event = {
            "captured_at": "2026-05-22T10:09:30+08:00",
            "display_id": "main",
            "app_name": "Obsidian",
            "bundle_id": "md.obsidian",
            "window_title": "candidate-note.md",
            "ocr_text": "document candidate visible semantic text",
            "context_confidence": 0.9,
        }

        with patch.dict(os.environ, {"PAM_PRIVACY_PROFILE": "strict"}, clear=False):
            self.assertIsNone(build_document_signal(self.root, event))
        with patch.dict(
            os.environ,
            {
                "PAM_PRIVACY_PROFILE": "personal_high_precision",
                "PAM_PERSONAL_DOCUMENT_ROOTS": str(docs),
            },
            clear=False,
        ):
            signal = build_document_signal(self.root, event)

        self.assertIsNone(signal)


    def test_default_personal_high_precision_matches_obsidian_resume_markdown(self) -> None:
        docs = self.root / "docs"
        vault = docs / "Obsidian Vault" / "Interview"
        vault.mkdir(parents=True)
        resume = vault / "Alex 简历 产品经理.md"
        resume.write_text(
            "Alex 简历 产品经理 项目经理 端到端交付 业务分析 面试材料\n"
            "最近正在整理商业化增长和 AI 工具工作流案例。",
            encoding="utf-8",
        )
        event = {
            "captured_at": "2026-06-09T12:00:00+08:00",
            "display_id": "main",
            "app_name": "Obsidian",
            "bundle_id": "md.obsidian",
            "window_title": "面试准备 - Obsidian",
            "ocr_text": "项目经理 端到端交付 业务分析 面试材料",
            "context_confidence": 0.9,
        }
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        with patch("activity_mvp.document_signals.Path.home", return_value=empty_home), patch.dict(
            os.environ,
            {"PAM_PRIVACY_PROFILE": "", "PAM_PERSONAL_DOCUMENT_ROOTS": str(docs)},
            clear=False,
        ):
            signal = build_document_signal(self.root, event)
        self.assertIsNone(signal)


    def test_personal_document_candidate_prefers_recent_text_match(self) -> None:
        docs = self.root / "docs"
        docs.mkdir()
        old_doc = docs / "resume-role-A.md"
        new_doc = docs / "resume-role-B.md"
        old_doc.write_text("resume role notes without the current visible details", encoding="utf-8")
        new_doc.write_text(
            "resume role portfolio operations leadership end to end delivery interview notes",
            encoding="utf-8",
        )
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        new_time = datetime(2026, 6, 9, tzinfo=timezone.utc).timestamp()
        os.utime(old_doc, (old_time, old_time))
        os.utime(new_doc, (new_time, new_time))
        event = {
            "captured_at": "2026-06-09T12:05:00+08:00",
            "display_id": "main",
            "app_name": "Obsidian",
            "window_title": "resume role - Obsidian",
            "ocr_text": "portfolio operations leadership end to end delivery",
        }
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        with patch("activity_mvp.document_signals.Path.home", return_value=empty_home), patch.dict(
            os.environ,
            {"PAM_PRIVACY_PROFILE": "personal_high_precision", "PAM_PERSONAL_DOCUMENT_ROOTS": str(docs)},
            clear=False,
        ):
            signal = build_document_signal(self.root, event)
        self.assertIsNone(signal)


    def test_personal_document_candidate_rejects_hidden_and_sensitive_files(self) -> None:
        docs = self.root / "docs"
        hidden_dir = docs / ".trash"
        hidden_dir.mkdir(parents=True)
        (hidden_dir / "resume.md").write_text("resume hidden interview details", encoding="utf-8")
        (docs / "resume.csv").write_text("resume,csv,interview", encoding="utf-8")
        (docs / "resume.key").write_text("secret", encoding="utf-8")
        event = {
            "captured_at": "2026-06-09T12:10:00+08:00",
            "display_id": "main",
            "app_name": "Obsidian",
            "window_title": "resume - Obsidian",
            "ocr_text": "resume hidden interview details",
        }
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        with patch("activity_mvp.document_signals.Path.home", return_value=empty_home), patch.dict(
            os.environ,
            {"PAM_PRIVACY_PROFILE": "personal_high_precision", "PAM_PERSONAL_DOCUMENT_ROOTS": str(docs)},
            clear=False,
        ):
            self.assertIsNone(build_document_signal(self.root, event))


    def test_sensitive_document_type_rejected(self) -> None:
        secret_path = self.root / "secret.pem"
        secret_path.write_text(
            SYNTHETIC_PRIVATE_KEY_HEADER + "\nsecret\n",
            encoding="utf-8",
        )
        signal = build_document_signal(
            self.root,
            {
                "captured_at": "2026-05-22T10:08:00+08:00",
                "app_name": "TextEdit",
                "window_title": "secret.pem",
                "ocr_text": "secret",
                "document_path": str(secret_path),
            },
        )
        self.assertEqual(signal["status"], "rejected")


    def test_sensitive_sidebar_labels_are_partially_redacted(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:10:00+08:00",
                "display_id": "main",
                "app_name": "Chrome",
                "bundle_id": "com.google.Chrome",
                "window_title": "普通网页",
                "url": "https://example.com/article",
                "domain": "example.com",
                "ocr_text": "币安 中国银行 Personal AI Monitor 普通网页内容",
                "ocr_blocks": [
                    {
                        "text": "币安",
                        "confidence": 0.96,
                        "bounding_box": {"x": 0.02, "y": 0.91, "width": 0.08, "height": 0.04},
                    },
                    {
                        "text": "中国银行",
                        "confidence": 0.94,
                        "bounding_box": {"x": 0.03, "y": 0.45, "width": 0.14, "height": 0.04},
                    },
                    {
                        "text": "Personal AI Monitor 普通网页内容",
                        "confidence": 0.97,
                        "bounding_box": {"x": 0.36, "y": 0.45, "width": 0.5, "height": 0.12},
                    },
                ],
                "context_confidence": 0.9,
            }
        ]

        report = ingest_events(self.conn, events)
        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["partial_redacted"], 1)
        self.assertEqual(report["blocked"], 0)
        self.assertEqual(report["review_required"], 0)
        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["privacy_level"], "partial_redacted")
        self.assertIn("局部敏感内容已清洗", activities[0]["activity_summary"])
        self.assertIn("Personal AI Monitor", activities[0]["activity_summary"])
        self.assertEqual(search(self.conn, "币安"), [])
        self.assertEqual(search(self.conn, "中国银行"), [])
        self.assertEqual(len(search(self.conn, "Personal AI Monitor")), 1)
        package_result = generate_package(
            self.conn,
            "2026-05-22T10:10:00+08:00",
            "2026-05-22T10:12:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")
        self.assertIn("partial_redacted", package_text)
        self.assertIn("unverified_clue", package_text)
        self.assertNotIn("Personal AI Monitor", package_text)
        self.assertNotIn("币安", package_text)
        self.assertNotIn("中国银行", package_text)


    def test_financial_account_main_content_blocks(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:11:00+08:00",
                "display_id": "main",
                "app_name": "Chrome",
                "bundle_id": "com.google.Chrome",
                "window_title": "Dashboard",
                "url": "https://example.com/dashboard",
                "domain": "example.com",
                "ocr_text": "币安 账户 余额 1000 USDT 交易 提现",
                "ocr_blocks": [
                    {
                        "text": "币安 账户 余额 1000 USDT 交易 提现",
                        "confidence": 0.96,
                        "bounding_box": {"x": 0.35, "y": 0.42, "width": 0.5, "height": 0.2},
                    }
                ],
                "context_confidence": 0.9,
            }
        ]

        report = ingest_events(self.conn, events)
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])
        self.assertEqual(search(self.conn, "USDT"), [])


    def test_token_login_discussion_is_not_whole_frame_blocked(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:12:00+08:00",
                "display_id": "main",
                "app_name": "Claude",
                "bundle_id": "com.anthropic.claude",
                "window_title": "项目隐私规则讨论",
                "ocr_text": "讨论 token login credential 概念，不包含真实值或赋值。",
                "context_confidence": 0.9,
            }
        ]

        with patch.dict(os.environ, {"PAM_PRIVACY_PROFILE": "personal_high_precision"}, clear=False):
            report = ingest_events(self.conn, events)
        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["partial_redacted"], 0)
        self.assertEqual(report["blocked"], 0)
        activities = timeline(self.conn)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["privacy_level"], "normal")
        self.assertNotIn("局部敏感内容已清洗", activities[0]["activity_summary"])
        self.assertEqual(len(search(self.conn, "token")), 1)
        self.assertEqual(len(search(self.conn, "credential")), 1)


    def test_soft_high_risk_words_use_english_boundaries(self) -> None:
        for text in ("secretary planning notes", "signing workflow", "tokenizer benchmark"):
            result = classify_event(
                {
                    "captured_at": "2026-05-22T10:12:30+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "notes",
                    "ocr_text": text,
                    "context_confidence": 0.9,
                }
            )
            self.assertEqual(result.status, "clear")

        for text in ("secret rotation notes", "sign in flow", "access token discussion"):
            with patch.dict(os.environ, {"PAM_PRIVACY_PROFILE": "strict"}, clear=False):
                strict_result = classify_event(
                    {
                        "captured_at": "2026-05-22T10:12:31+08:00",
                        "display_id": "main",
                        "app_name": "Obsidian",
                        "window_title": "notes",
                        "ocr_text": text,
                        "context_confidence": 0.9,
                    }
                )
            self.assertEqual(strict_result.status, "partial_redacted")

            with patch.dict(os.environ, {"PAM_PRIVACY_PROFILE": "personal_high_precision"}, clear=False):
                personal_result = classify_event(
                    {
                        "captured_at": "2026-05-22T10:12:31+08:00",
                        "display_id": "main",
                        "app_name": "Obsidian",
                        "window_title": "notes",
                        "ocr_text": text,
                        "context_confidence": 0.9,
                    }
                )
            self.assertEqual(personal_result.status, "clear")

            result = classify_event(
                {
                    "captured_at": "2026-05-22T10:12:32+08:00",
                    "display_id": "main",
                    "app_name": "Chrome",
                    "window_title": "notes",
                    "ocr_text": text,
                    "context_confidence": 0.9,
                }
            )
            self.assertEqual(result.status, "partial_redacted")


    def test_plain_long_ids_are_preserved_for_personal_accuracy(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:12:40+08:00",
                "display_id": "main",
                "app_name": "Obsidian",
                "bundle_id": "md.obsidian",
                "window_title": "订单整理",
                "ocr_text": "物流单号 1234567890123 和项目编号 9876543210988 需要核对。",
                "context_confidence": 0.9,
            }
        ]

        report = ingest_events(self.conn, events)
        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["partial_redacted"], 0)
        activities = timeline(self.conn)
        self.assertIn("1234567890123", activities[0]["cleaned_text"])
        self.assertIn("9876543210988", activities[0]["cleaned_text"])
        self.assertEqual(len(search(self.conn, "1234567890123")), 1)


    def test_exact_secret_pattern_still_blocks(self) -> None:
        events = [
            {
                "captured_at": "2026-05-22T10:13:00+08:00",
                "display_id": "main",
                "app_name": "Notes",
                "window_title": "配置片段",
                "ocr_text": "api_key = " + SYNTHETIC_SECRET_TOKEN,
                "context_confidence": 0.9,
            }
        ]

        report = ingest_events(self.conn, events)
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])


    def test_quality_report_feedback_redacts_and_cascades_on_delete(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:14:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "quality report",
                    "ocr_text": "Local Owner is implementing the Personal AI Monitor quality report and rerunning tests.",
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                }
            ],
        )
        activity = timeline(self.conn)[0]
        feedback = add_quality_feedback(
            self.conn,
            target_type="activity",
            target_id=activity["id"],
            issue_type="wrong_workflow",
            expected_status="clear",
            note="check /Users/example/private and " + SYNTHETIC_SECRET_TOKEN,
        )
        self.assertIn("[path_redacted]", feedback["note_redacted"])
        self.assertIn("[secret_redacted]", feedback["note_redacted"])

        report = quality_report(
            self.conn,
            "2026-05-22T10:13:00+08:00",
            "2026-05-22T10:15:00+08:00",
        )
        markdown = render_quality_markdown(report)
        self.assertEqual(report["feedback"]["count"], 1)
        self.assertGreaterEqual(report["learning"]["memory_candidate_count"], 1)
        self.assertIn("memory_write_health", report)
        self.assertIn("eligible_candidate_count", report["memory_write_health"])
        self.assertIn("subject_risk_candidate_count", report["memory_write_health"])
        self.assertIn("write_guard_summary", report["memory_write_health"])
        self.assertIn("short_content_buckets", report["cleaning"])
        self.assertIn("长期记忆写入健康", markdown)
        self.assertIn("长期记忆写入审计", markdown)
        self.assertIn("主体风险合计", markdown)
        self.assertIn("third_party_subject / ambiguous_subject 不进入 Hermes 写入输入", markdown)
        self.assertIn("single_evidence_demoted_count", json.dumps(report, ensure_ascii=False))
        self.assertIn("quality", json.dumps(report, ensure_ascii=False).lower())
        self.assertNotIn("/Users/example", markdown)
        self.assertNotIn(SYNTHETIC_SECRET_TOKEN, markdown)

        deleted = delete_range(
            self.conn,
            "2026-05-22T10:13:00+08:00",
            "2026-05-22T10:15:00+08:00",
        )
        self.assertEqual(deleted["cascade"]["quality_feedback"], 1)
        self.assertEqual(list_quality_feedback(self.conn), [])
        self.assertEqual(search(self.conn, SYNTHETIC_SECRET_TOKEN), [])


    def test_quality_report_includes_attribution_health_without_raw_detail(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:20:00+08:00",
                    "display_id": "external-1",
                    "app_name": "Unknown",
                    "window_title": "untitled",
                    "ocr_text": "Review Personal AI Monitor attribution quality and extension health.",
                    "context_confidence": 0.3,
                },
                {
                    "captured_at": "2026-05-22T10:20:30+08:00",
                    "display_id": "main",
                    "app_name": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "window_title": "Research",
                    "url": "https://research.example/[redacted]",
                    "domain": "research.example",
                    "url_source": "chromium_extension_native_host",
                    "ocr_text": "Extension browser attribution count",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T10:20:40+08:00",
                    "display_id": "main",
                    "app_name": "Google Chrome",
                    "bundle_id": "com.google.Chrome",
                    "window_title": "Fallback",
                    "url": "https://fallback.example/[redacted]",
                    "domain": "fallback.example",
                    "url_source": "applescript_chrome",
                    "ocr_text": "AppleScript fallback attribution count",
                    "context_confidence": 0.8,
                }
            ],
        )
        report = quality_report(
            self.conn,
            "2026-05-22T10:19:00+08:00",
            "2026-05-22T10:21:00+08:00",
        )
        attribution = report["attribution_health"]
        self.assertEqual(attribution["cross_display_inheritance"], "disabled")
        self.assertGreaterEqual(attribution["frame_signal_count"], 1)
        self.assertGreaterEqual(attribution["unknown_app_frame_count"], 1)
        self.assertGreaterEqual(attribution["secondary_display_frame_count"], 1)
        self.assertGreaterEqual(attribution["extension_browser_signal_count"], 1)
        self.assertGreaterEqual(attribution["applescript_browser_signal_count"], 1)
        self.assertIn("confidence_label", attribution)
        self.assertIn("source_buckets", attribution)
        encoded = json.dumps(attribution, ensure_ascii=False)
        self.assertNotIn("ocr_text", encoded)
        self.assertNotIn("window_title", encoded)


    def test_attribution_health_uses_display_attribution_for_sck_displays(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-04T12:00:00+08:00",
                    "display_id": "sck-display-1",
                    "app_name": "Codex",
                    "bundle_id": "com.openai.codex",
                    "window_title": "safe work title",
                    "ocr_text": "editing attribution tests",
                    "context_source": "screencapturekit_keyframe+frontmost",
                    "display_attribution": {
                        "app_name": "Codex",
                        "bundle_id": "com.openai.codex",
                        "confidence": 0.78,
                        "source": "screencapturekit_keyframe+frontmost",
                        "reason_codes": ["main_display_frontmost"],
                    },
                },
                {
                    "captured_at": "2026-07-04T12:00:00+08:00",
                    "display_id": "sck-display-3",
                    "app_name": "Unknown",
                    "window_title": "",
                    "ocr_text": "empty secondary attribution",
                    "context_confidence": 0.32,
                    "context_source": "screencapturekit_keyframe+display_window_list_empty",
                    "display_attribution": {
                        "app_name": "Unknown",
                        "confidence": 0.32,
                        "source": "screencapturekit_keyframe+display_window_list_empty",
                        "reason_codes": ["display_window_list_empty"],
                    },
                },
            ],
        )

        attribution = quality_report(
            self.conn,
            "2026-07-04T11:59:00+08:00",
            "2026-07-04T12:01:00+08:00",
        )["attribution_health"]

        self.assertEqual(attribution["main_display_frame_count"], 1)
        self.assertEqual(attribution["secondary_display_frame_count"], 1)
        self.assertEqual(attribution["unknown_display_frame_count"], 0)
        reasons = {item["reason"]: item for item in attribution["attribution_reason_counts"]}
        self.assertIn("display_window_list_empty", reasons)
        self.assertNotEqual(attribution["secondary_display_frame_count"], attribution["frame_signal_count"])


    def test_memory_audit_reports_suspicious_pam_memories_without_raw_residue(self) -> None:
        fake_home = self.root / "fake-home"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        mem0 = bin_dir / "mem0"
        payload = {
            "ok": True,
            "results": [
                {
                    "id": "mem-1",
                    "memory": (
                        "IINA file listing video.mp4 single_evidence source_system=personal_ai_monitor "
                        "source_platform=macos_live_runtime package_id=ha_pkg_test /Users/example/private "
                        f"user@example.com {SYNTHETIC_SECRET_TOKEN}"
                    ),
                    "metadata": {
                        "source_system": "personal_ai_monitor",
                        "source_platform": "macos_live_runtime",
                        "source_profile": "activity-analysis",
                        "package_id": "ha_pkg_test",
                        "hour_key": "2026-06-10T04:00:00+00:00",
                        "user_id": "local-owner",
                        "event_type": "ha_hourly_activity_analysis",
                    },
                    "created_at": "2026-06-10T05:01:00+00:00",
                    "updated_at": "2026-06-10T05:01:00+00:00",
                }
            ],
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print({json.dumps(json.dumps(payload), ensure_ascii=False)!r})\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)

        with patch("activity_mvp.memory_audit.Path.home", return_value=fake_home):
            report = memory_audit("2026-06-10T00:00:00+00:00", "2026-06-11T00:00:00+00:00")

        encoded = json.dumps(report, ensure_ascii=False)
        markdown = render_memory_audit_markdown(report)
        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["destructive_actions"])
        self.assertEqual(report["item_count"], 1)
        self.assertIn("weak_single_evidence", report["items"][0]["issue_types"])
        self.assertIn("media_or_file_listing_noise", report["items"][0]["issue_types"])
        self.assertNotIn("/Users/example", encoded)
        self.assertNotIn("user@example.com", encoded)
        self.assertNotIn(SYNTHETIC_SECRET_TOKEN, encoded)
        self.assertIn("只读取 mem0", markdown)


    def test_memory_audit_reports_third_party_subject_risk_read_only(self) -> None:
        fake_home = self.root / "fake-home-third-party-memory"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        mem0 = bin_dir / "mem0"
        payload = {
            "ok": True,
            "results": [
                {
                    "id": "mem-third-party",
                    "memory": (
                        "候选人 张三 简历 工作经历 教育经历 source_system=personal_ai_monitor "
                        "source_platform=macos_live_runtime package_id=ha_pkg_test"
                    ),
                    "metadata": {
                        "source_system": "personal_ai_monitor",
                        "source_platform": "macos_live_runtime",
                        "source_profile": "activity-analysis",
                        "package_id": "ha_pkg_test",
                        "hour_key": "2026-06-10T04:00:00+00:00",
                        "user_id": "local-owner",
                        "event_type": "ha_hourly_activity_analysis",
                    },
                    "created_at": "2026-06-10T05:01:00+00:00",
                    "updated_at": "2026-06-10T05:01:00+00:00",
                }
            ],
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print({json.dumps(json.dumps(payload), ensure_ascii=False)!r})\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)

        with patch("activity_mvp.memory_audit.Path.home", return_value=fake_home):
            report = memory_audit("2026-06-10T00:00:00+00:00", "2026-06-11T00:00:00+00:00")

        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["destructive_actions"])
        self.assertEqual(report["item_count"], 1)
        self.assertIn("third_party_subject_risk", report["items"][0]["issue_types"])


    def test_memory_audit_flags_personal_medical_memory_without_echoing_details(self) -> None:
        fake_home = self.root / "fake-home-medical-memory"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        mem0 = bin_dir / "mem0"
        payload = {
            "ok": True,
            "results": [
                {
                    "id": "mem-medical",
                    "memory": (
                        "我的体检报告里血常规异常，医生建议复诊并讨论用药。"
                        " source_system=personal_ai_monitor source_platform=macos_live_runtime package_id=ha_pkg_medical"
                    ),
                    "metadata": {
                        "source_system": "personal_ai_monitor",
                        "source_platform": "macos_live_runtime",
                        "source_profile": "activity-analysis",
                        "package_id": "ha_pkg_medical",
                        "hour_key": "2026-06-14T02:00:00+00:00",
                        "user_id": "local-owner",
                        "event_type": "ha_hourly_activity_analysis",
                    },
                    "created_at": "2026-06-14T03:01:00+00:00",
                    "updated_at": "2026-06-14T03:01:00+00:00",
                }
            ],
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print({json.dumps(json.dumps(payload), ensure_ascii=False)!r})\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)

        with patch("activity_mvp.memory_audit.Path.home", return_value=fake_home):
            report = memory_audit("2026-06-14T00:00:00+00:00", "2026-06-15T00:00:00+00:00")

        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["item_count"], 1)
        self.assertIn("personal_medical_content_risk", report["items"][0]["issue_types"])
        self.assertIn("个人临床医疗长期记忆可疑项", report["items"][0]["safe_summary"])
        self.assertNotIn("血常规", encoded)
        self.assertNotIn("复诊并讨论", encoded)


    def test_memory_audit_scans_profile_memory_for_jd_self_positioning_risk(self) -> None:
        fake_home = self.root / "fake-home-profile-memory"
        memory_dir = fake_home / ".hermes" / "profiles" / "feishubot" / "memories"
        memory_dir.mkdir(parents=True)
        (memory_dir / "USER.md").write_text(
            "Local Owner 的核心能力是 EMEA/APAC 区域渠道打法和 JD 任职要求中的销售策略。\n"
            "§\n"
            "职业定位纠偏：EMEA/APAC 若来自 JD，只能作为岗位适配项，不应写成自有强项。\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_audit.Path.home", return_value=fake_home):
            report = memory_audit(limit=20)

        self.assertEqual(report["status"], "ok")
        self.assertIn("hermes_profile_memory", report["source"])
        self.assertEqual(report["item_count"], 1)
        item = report["items"][0]
        self.assertIn("jd_requirement_as_self_positioning_risk", item["issue_types"])
        self.assertNotIn("missing_metadata", item["issue_types"])
        self.assertIn("复核是否应改写", item["recommendation"])


    def test_memory_audit_does_not_flag_self_resume_safe_summary_as_jd_risk(self) -> None:
        fake_home = self.root / "fake-home-self-resume-safe-memory"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        mem0 = bin_dir / "mem0"
        payload = {
            "ok": True,
            "results": [
                {
                    "id": "mem-self-resume-safe",
                    "memory": (
                        "Local Owner 的自用简历安全摘要显示：其自我定位包含 GTM、新品上市、KOL、跨境电商与 AI 工具工作流经验。"
                    ),
                    "metadata": {
                        "source_system": "personal_ai_monitor",
                        "source_platform": "macos_live_runtime",
                        "source_profile": "activity-analysis",
                        "package_id": "ha_pkg_self_resume",
                        "hour_key": "2026-06-13T13:00:00+00:00",
                        "user_id": "local-owner",
                        "event_type": "ha_hourly_activity_analysis",
                        "memory_user_id": "local-owner",
                        "subject_scope": "self_or_user_owned",
                        "source_category": "self_resume_safe_summary",
                        "memory_write_policy": "self_resume_safe_summary",
                        "memory_scope": "safe_summary",
                    },
                    "user_id": "local-owner",
                    "created_at": "2026-06-13T14:46:00+00:00",
                }
            ],
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print({json.dumps(json.dumps(payload), ensure_ascii=False)!r})\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)

        with patch("activity_mvp.memory_audit.Path.home", return_value=fake_home):
            report = memory_audit(limit=20)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["item_count"], 0)


    def test_memory_ledger_refresh_is_frozen_and_creates_no_new_export(self) -> None:
        fake_home = self.root / "fake-home-ledger-refresh"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        mem0 = bin_dir / "mem0"
        payload = {
            "ok": True,
            "memories": {
                "results": [
                    {
                        "id": "mem-pam",
                        "memory": (
                            "Personal AI Monitor 写入安全摘要 /Users/example/private "
                            "user@example.com +1 415 555 1212 "
                            f"{SYNTHETIC_SECRET_TOKEN}"
                        ),
                        "hash": "hash-pam",
                        "metadata": {
                            "source_system": "personal_ai_monitor",
                            "source_platform": "macos_live_runtime",
                            "source_profile": "activity-analysis",
                            "package_id": "ha_pkg_test",
                            "hour_key": "2026-06-10T04:00:00+00:00",
                            "memory_user_id": "local-owner",
                            "subject_scope": "self_or_user_owned",
                        },
                        "user_id": "local-owner",
                        "created_at": "2026-06-10T05:01:00+00:00",
                    },
                    {
                        "id": "mem-other",
                        "memory": "Unrelated non-PAM memory",
                        "metadata": {"source_system": "codex"},
                        "user_id": "local-owner",
                    },
                ]
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print({json.dumps(payload, ensure_ascii=False)!r})\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        self.conn.execute(
            """
            INSERT INTO ha_automation_jobs(
              id, hour_key, started_at, ended_at, package_id, activity_count,
              safe_timeline_event_count, trigger_mode, hermes_profile, status,
              memories_written_count, written_memory_summaries, write_audit_summary,
              created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-ledger",
                "2026-06-10T04:00:00+00:00",
                "2026-06-10T04:00:00+00:00",
                "2026-06-10T05:00:00+00:00",
                "ha_pkg_legacy",
                1,
                1,
                "direct_hermes",
                "activity-analysis",
                "completed",
                1,
                json.dumps(["历史写入摘要，只作为线索。"], ensure_ascii=False),
                "safe",
                "2026-06-10T05:01:00+00:00",
                "2026-06-10T05:01:00+00:00",
            ),
        )
        self.conn.commit()
        ledger_path = fake_home / "Obsidian" / "PAM 长期记忆台账.md"

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home):
            report = refresh_memory_ledger(self.conn, ledger_path=ledger_path)
            status = memory_ledger_status(self.conn, ledger_path=ledger_path)

        detail_path = Path(report["detail_path"])
        self.assertEqual(report["status"], "frozen_legacy")
        self.assertEqual(report["action_status"], "blocked_read_only")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["write_enabled"])
        self.assertFalse(report["destructive_actions"])
        self.assertEqual(report["mem0_entry_count"], 0)
        self.assertEqual(report["ha_summary_entry_count"], 0)
        self.assertEqual(status["status"], "frozen_legacy")
        self.assertTrue(status["read_only"])
        self.assertFalse(status["write_enabled"])
        self.assertFalse(ledger_path.exists())
        self.assertFalse(detail_path.exists())


    def test_memory_ledger_sync_deleted_line_is_blocked_read_only(self) -> None:
        fake_home = self.root / "fake-home-ledger-delete"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        current = {
            "ok": True,
            "memory": {
                "id": "mem-delete",
                "memory": "PAM safe summary",
                "hash": "hash-delete",
                "metadata": {
                    "source_system": "personal_ai_monitor",
                    "source_platform": "macos_live_runtime",
                    "memory_user_id": "local-owner",
                    "package_id": "ha_pkg_delete",
                },
                "user_id": "local-owner",
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"current = {json.dumps(current, ensure_ascii=False)!r}\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get':\n"
            "    print(current)\n"
            "elif cmd == 'delete':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'id': sys.argv[2]}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True, 'result': {'message': 'deleted'}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "ledger.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text(
            "# PAM 长期记忆台账\n\n"
            "最后刷新：2026-06-13 23:00\n\n",
            encoding="utf-8",
        )
        detail_path.write_text(
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-delete\n"
            "entry_type: mem0\n"
            "can_sync: true\n"
            "memory_id: mem-delete\n"
            "current_hash: hash-delete\n"
            "user_id: local-owner\n"
            "source_system: personal_ai_monitor\n"
            "source_platform: macos_live_runtime\n"
            "source_profile: activity-analysis\n"
            "package_id: ha_pkg_delete\n"
            "hour_key: 2026-06-10T04:00:00+00:00\n"
            "subject_scope: self_or_user_owned\n"
            "source_category: self_resume\n"
            "memory_scope: safe_summary\n"
            "memory_write_policy: self_resume_safe_summary\n"
            "sync_action: keep\n"
            "sync_confirm:\n"
            "rewrite_memory:\n"
            "review_note:\n"
            "sync_status: ready\n"
            "last_synced_at:\n"
            "```\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home):
            deleted = sync_memory_ledger(self.conn, ledger_path=ledger_path, backup_dir=fake_home / "backups")

        self.assertEqual(deleted["status"], "frozen_legacy")
        self.assertEqual(deleted["action_status"], "blocked_read_only")
        self.assertEqual(deleted["results"][0]["status"], "blocked_read_only")
        self.assertEqual(deleted["deleted_count"], 0)
        self.assertEqual(deleted["processed_count"], 0)
        self.assertEqual(deleted["backup_path"], "")
        self.assertFalse(log_path.exists())
        self.assertIn("最后刷新", ledger_path.read_text(encoding="utf-8"))
        self.assertIn("sync_status: ready", detail_path.read_text(encoding="utf-8"))


    def test_memory_ledger_sync_hash_mismatch_is_still_blocked_read_only(self) -> None:
        fake_home = self.root / "fake-home-ledger-hash-mismatch"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        current = {
            "ok": True,
            "memory": {
                "id": "mem-delete",
                "memory": "PAM safe summary",
                "hash": "hash-delete",
                "metadata": {
                    "source_system": "personal_ai_monitor",
                    "source_platform": "macos_live_runtime",
                    "memory_user_id": "local-owner",
                    "package_id": "ha_pkg_delete",
                },
                "user_id": "local-owner",
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"current = {json.dumps(current, ensure_ascii=False)!r}\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get':\n"
            "    print(current)\n"
            "elif cmd == 'delete':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'id': sys.argv[2]}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True, 'result': {'message': 'deleted'}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "ledger.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text("# PAM 长期记忆台账\n\n最后刷新：2026-06-13 23:00\n\n", encoding="utf-8")
        detail_path.write_text(
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-delete\n"
            "entry_type: mem0\n"
            "can_sync: true\n"
            "memory_id: mem-delete\n"
            "current_hash: wrong-hash\n"
            "memory_safe: PAM safe summary\n"
            "user_id: local-owner\n"
            "source_system: personal_ai_monitor\n"
            "source_platform: macos_live_runtime\n"
            "source_profile: activity-analysis\n"
            "package_id: ha_pkg_delete\n"
            "hour_key: 2026-06-10T04:00:00+00:00\n"
            "subject_scope: self_or_user_owned\n"
            "source_category: self_resume\n"
            "memory_scope: safe_summary\n"
            "memory_write_policy: self_resume_safe_summary\n"
            "sync_action: keep\n"
            "sync_confirm:\n"
            "rewrite_memory:\n"
            "review_note:\n"
            "sync_status: ready\n"
            "last_synced_at:\n"
            "created_at:\n"
            "```\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home):
            mismatch = sync_memory_ledger(self.conn, ledger_path=ledger_path, backup_dir=fake_home / "backups")
        self.assertEqual(mismatch["status"], "frozen_legacy")
        self.assertEqual(mismatch["action_status"], "blocked_read_only")
        self.assertEqual(mismatch["results"][0]["status"], "blocked_read_only")
        self.assertEqual(mismatch["deleted_count"], 0)
        self.assertFalse(log_path.exists())


    def test_memory_ledger_sync_rewritten_line_cannot_update_memory(self) -> None:
        fake_home = self.root / "fake-home-ledger-rewrite"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        memories = {
            "mem-rewrite": {
                "id": "mem-rewrite",
                "memory": "old PAM summary",
                "hash": "hash-rewrite",
                "metadata": {
                    "source_system": "personal_ai_monitor",
                    "source_platform": "macos_live_runtime",
                    "memory_user_id": "local-owner",
                    "package_id": "ha_pkg_rewrite",
                    "subject_scope": "self_or_user_owned",
                },
                "user_id": "local-owner",
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"memories = json.loads({json.dumps(memories, ensure_ascii=False)!r})\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get':\n"
            "    print(json.dumps({'ok': True, 'memory': memories[sys.argv[2]]}, ensure_ascii=False))\n"
            "elif cmd == 'update':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'id': sys.argv[2], 'text': sys.argv[3], 'metadata': json.loads(sys.argv[4])}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True, 'result': {'message': 'updated'}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "ledger.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text(
            "# PAM 长期记忆台账\n\n"
            "最后刷新：2026-06-13 23:00\n\n"
            "1. 改写后的安全摘要 /Users/example/private user@example.com "
            f"{SYNTHETIC_SECRET_TOKEN}\n",
            encoding="utf-8",
        )
        detail_path.write_text(
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-rewrite\n"
            "entry_type: mem0\n"
            "can_sync: true\n"
            "memory_id: mem-rewrite\n"
            "current_hash: hash-rewrite\n"
            "memory_safe: old PAM summary\n"
            "user_id: local-owner\n"
            "source_system: personal_ai_monitor\n"
            "source_platform: macos_live_runtime\n"
            "source_profile: activity-analysis\n"
            "package_id: ha_pkg_rewrite\n"
            "hour_key:\n"
            "subject_scope: self_or_user_owned\n"
            "source_category: self_resume\n"
            "memory_scope: safe_summary\n"
            "memory_write_policy: self_resume_safe_summary\n"
            "sync_action: keep\n"
            "sync_confirm:\n"
            "rewrite_memory:\n"
            "review_note:\n"
            "sync_status: ready\n"
            "last_synced_at:\n"
            "```\n\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home):
            result = sync_memory_ledger(self.conn, ledger_path=ledger_path, backup_dir=fake_home / "backups")

        self.assertEqual(result["status"], "frozen_legacy")
        self.assertEqual(result["action_status"], "blocked_read_only")
        self.assertEqual(result["rewritten_count"], 0)
        self.assertEqual(result["processed_count"], 0)
        self.assertEqual(result["results"][0]["status"], "blocked_read_only")
        self.assertFalse(log_path.exists())
        self.assertIn("/Users/example/private", ledger_path.read_text(encoding="utf-8"))


    def test_memory_ledger_sync_rewritten_non_pam_entry_is_blocked(self) -> None:
        fake_home = self.root / "fake-home-ledger-non-pam"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        memory = {
            "id": "mem-other",
            "memory": "old non PAM summary",
            "hash": "hash-other",
            "metadata": {"source_system": "codex"},
            "user_id": "local-owner",
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"memory = json.loads({json.dumps(memory, ensure_ascii=False)!r})\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get': print(json.dumps({'ok': True, 'memory': memory}, ensure_ascii=False))\n"
            "elif cmd == 'update':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'id': sys.argv[2]}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True}))\n"
            "else: print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "ledger.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text("# PAM 长期记忆台账\n\n最后刷新：2026-06-13 23:00\n\n1. should not update\n", encoding="utf-8")
        detail_path.write_text(
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-other\nentry_type: mem0\ncan_sync: true\nmemory_id: mem-other\ncurrent_hash: hash-other\nmemory_safe: old non PAM summary\nuser_id: local-owner\nsource_system: codex\nsource_platform: local\nsource_profile: codex\npackage_id:\nhour_key:\nsubject_scope:\nsource_category:\nmemory_scope:\nmemory_write_policy:\nsync_action: keep\nsync_confirm:\nrewrite_memory:\nreview_note:\nsync_status: ready\nlast_synced_at:\ncreated_at:\n"
            "```\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home):
            result = sync_memory_ledger(self.conn, ledger_path=ledger_path, backup_dir=fake_home / "backups")

        self.assertEqual(result["status"], "frozen_legacy")
        self.assertEqual(result["action_status"], "blocked_read_only")
        self.assertEqual(result["results"][0]["status"], "blocked_read_only")
        self.assertFalse(log_path.exists())


    def test_memory_ledger_sync_new_line_and_complex_replace_do_not_mutate(self) -> None:
        fake_home = self.root / "fake-home-ledger-complex"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        memories = {
            "mem-a": {
                "id": "mem-a",
                "memory": "old A",
                "hash": "hash-a",
                "metadata": {"source_system": "personal_ai_monitor", "source_platform": "macos_live_runtime", "memory_user_id": "local-owner"},
                "user_id": "local-owner",
            },
            "mem-b": {
                "id": "mem-b",
                "memory": "old B",
                "hash": "hash-b",
                "metadata": {"source_system": "personal_ai_monitor", "source_platform": "macos_live_runtime", "memory_user_id": "local-owner"},
                "user_id": "local-owner",
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"memories = json.loads({json.dumps(memories, ensure_ascii=False)!r})\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get': print(json.dumps({'ok': True, 'memory': memories[sys.argv[2]]}, ensure_ascii=False))\n"
            "elif cmd in {'delete', 'update'}:\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'id': sys.argv[2]}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True}))\n"
            "else: print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "ledger.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text(
            "# PAM 长期记忆台账\n\n"
            "最后刷新：2026-06-13 23:00\n\n"
            "1. 新增自由文本\n"
            "2. replacement A\n"
            "3. replacement B\n",
            encoding="utf-8",
        )
        detail_path.write_text(
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-a\nentry_type: mem0\ncan_sync: true\nmemory_id: mem-a\ncurrent_hash: hash-a\nmemory_safe: old A\nuser_id: local-owner\nsource_system: personal_ai_monitor\nsource_platform: macos_live_runtime\nsource_profile: activity-analysis\npackage_id: ha_pkg_a\nhour_key:\nsubject_scope: self_or_user_owned\nsource_category: self_resume\nmemory_scope: safe_summary\nmemory_write_policy: self_resume_safe_summary\nsync_action: keep\nsync_confirm:\nrewrite_memory:\nreview_note:\nsync_status: ready\nlast_synced_at:\ncreated_at:\n"
            "```\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-b\nentry_type: mem0\ncan_sync: true\nmemory_id: mem-b\ncurrent_hash: hash-b\nmemory_safe: old B\nuser_id: local-owner\nsource_system: personal_ai_monitor\nsource_platform: macos_live_runtime\nsource_profile: activity-analysis\npackage_id: ha_pkg_b\nhour_key:\nsubject_scope: self_or_user_owned\nsource_category: self_resume\nmemory_scope: safe_summary\nmemory_write_policy: self_resume_safe_summary\nsync_action: keep\nsync_confirm:\nrewrite_memory:\nreview_note:\nsync_status: ready\nlast_synced_at:\ncreated_at:\n"
            "```\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home):
            result = sync_memory_ledger(self.conn, ledger_path=ledger_path, backup_dir=fake_home / "backups")

        self.assertEqual(result["status"], "frozen_legacy")
        self.assertEqual(result["action_status"], "blocked_read_only")
        self.assertEqual(result["unmatched_new_line_count"], 0)
        self.assertEqual(result["complex_change_count"], 0)
        self.assertEqual(result["processed_count"], 0)
        self.assertFalse(log_path.exists())


    def test_memory_approval_delete_is_blocked_for_frozen_legacy_ledger(self) -> None:
        fake_home = self.root / "fake-home-approval-delete"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        current = {
            "ok": True,
            "memory": {
                "id": "mem-delete",
                "memory": "PAM safe summary",
                "hash": "hash-delete",
                "metadata": {
                    "source_system": "personal_ai_monitor",
                    "source_platform": "macos_live_runtime",
                    "memory_user_id": "local-owner",
                },
                "user_id": "local-owner",
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"current = {json.dumps(current, ensure_ascii=False)!r}\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get': print(json.dumps(current, ensure_ascii=False))\n"
            "elif cmd == 'delete':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'id': sys.argv[2]}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True, 'result': {'message': 'deleted'}}))\n"
            "else: print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "Documents/Obsidian Vault/✦ AI 工作台/PAM 长期记忆台账.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        ledger_path.parent.mkdir(parents=True)
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text("# PAM 长期记忆台账\n\n最后刷新：2026-06-15 15:00\n\n1. PAM safe summary\n", encoding="utf-8")
        detail_path.write_text(
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-delete\nentry_type: mem0\ncan_sync: true\nmemory_id: mem-delete\ncurrent_hash: hash-delete\nmemory_safe: PAM safe summary\nuser_id: local-owner\nsource_system: personal_ai_monitor\nsource_platform: macos_live_runtime\nsource_profile: activity-analysis\npackage_id: ha_pkg_delete\nhour_key:\nsubject_scope: self_or_user_owned\nsource_category: self_resume\nmemory_scope: safe_summary\nmemory_write_policy: self_resume_safe_summary\nsync_action: keep\nsync_confirm:\nrewrite_memory:\nreview_note:\nsync_status: ready\nlast_synced_at:\ncreated_at:\n"
            "```\n",
            encoding="utf-8",
        )

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home), patch(
            "activity_mvp.memory_approval.Path.home", return_value=fake_home
        ):
            preview = preview_memory_approval(
                self.conn,
                action="delete",
                ledger_id="mem0:mem-delete",
                plan_dir=fake_home / "approvals",
            )
        self.assertEqual(preview["status"], "frozen_legacy_read_only")
        self.assertFalse(preview["destructive_actions"])
        self.assertNotIn("plan_id", preview)
        self.assertFalse(log_path.exists())
        self.assertIn("PAM safe summary", ledger_path.read_text(encoding="utf-8"))


    def test_memory_approval_rewrite_is_blocked_before_hash_or_apply(self) -> None:
        fake_home = self.root / "fake-home-approval-hash-drift"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        current = {
            "ok": True,
            "memory": {
                "id": "mem-rewrite",
                "memory": "old PAM summary",
                "hash": "hash-rewrite",
                "metadata": {"source_system": "personal_ai_monitor", "source_platform": "macos_live_runtime", "memory_user_id": "local-owner"},
                "user_id": "local-owner",
            },
        }
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            f"current = {json.dumps(current, ensure_ascii=False)!r}\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'get': print(json.dumps(current, ensure_ascii=False))\n"
            "elif cmd == 'update':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True}))\n"
            "else: print(json.dumps({'ok': True, 'memories': {'results': []}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ledger_path = fake_home / "Documents/Obsidian Vault/✦ AI 工作台/PAM 长期记忆台账.md"
        detail_path = fake_home / "Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md"
        ledger_path.parent.mkdir(parents=True)
        detail_path.parent.mkdir(parents=True)
        ledger_path.write_text("# PAM 长期记忆台账\n\n最后刷新：2026-06-15 15:00\n\n1. old PAM summary\n", encoding="utf-8")
        detail = (
            "# PAM 长期记忆详细记录\n\n"
            "```pam-ledger\n"
            "ledger_id: mem0:mem-rewrite\nentry_type: mem0\ncan_sync: true\nmemory_id: mem-rewrite\ncurrent_hash: hash-rewrite\nmemory_safe: old PAM summary\nuser_id: local-owner\nsource_system: personal_ai_monitor\nsource_platform: macos_live_runtime\nsource_profile: activity-analysis\npackage_id: ha_pkg_rewrite\nhour_key:\nsubject_scope: self_or_user_owned\nsource_category: self_resume\nmemory_scope: safe_summary\nmemory_write_policy: self_resume_safe_summary\nsync_action: keep\nsync_confirm:\nrewrite_memory:\nreview_note:\nsync_status: ready\nlast_synced_at:\ncreated_at:\n"
            "```\n"
        )
        detail_path.write_text(detail, encoding="utf-8")

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home), patch(
            "activity_mvp.memory_approval.Path.home", return_value=fake_home
        ):
            preview = preview_memory_approval(
                self.conn,
                action="rewrite",
                ledger_id="mem0:mem-rewrite",
                replacement_text="new PAM safe summary",
                plan_dir=fake_home / "approvals",
            )
        self.assertEqual(preview["status"], "frozen_legacy_read_only")
        self.assertFalse(preview["destructive_actions"])
        self.assertNotIn("plan_id", preview)
        self.assertFalse(log_path.exists())


    def test_memory_approval_eligible_candidate_is_visible_but_writer_disabled(self) -> None:
        fake_home = self.root / "fake-home-approval-candidate"
        bin_dir = fake_home / "bin"
        bin_dir.mkdir(parents=True)
        log_path = fake_home / "mem0-calls.jsonl"
        mem0 = bin_dir / "mem0"
        mem0.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, pathlib\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'add':\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as fh: fh.write(json.dumps({'cmd': cmd, 'payload': json.loads(sys.argv[2])}, ensure_ascii=False) + '\\n')\n"
            "    print(json.dumps({'ok': True, 'memory': {'id': 'mem-new'}}))\n"
            "elif cmd in {'search', 'list'}:\n"
            "    print(json.dumps({'ok': True, 'memories': {'results': []}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True, 'memory': {}}))\n",
            encoding="utf-8",
        )
        mem0.chmod(0o755)
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-15T14:01:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Personal AI Monitor memory decision",
                    "ocr_text": "Decision: Local Owner confirms Personal AI Monitor v0.1.18 uses ledger-driven memory approvals for safe candidate writes.",
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-15T14:03:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Personal AI Monitor memory decision",
                    "ocr_text": "Decision confirmed by Local Owner: Personal AI Monitor v0.1.18 uses ledger-driven memory approvals for safe candidate writes.",
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Decision: Local Owner confirms Personal AI Monitor v0.1.18 uses ledger-driven memory approvals for safe candidate writes.",
            "2026-06-15T14:01:10+08:00",
        )
        self._manual_confirm(
            "Decision confirmed by Local Owner: Personal AI Monitor v0.1.18 uses ledger-driven memory approvals for safe candidate writes.",
            "2026-06-15T14:03:10+08:00",
        )
        package = generate_package(
            self.conn,
            "2026-06-15T14:00:00+08:00",
            "2026-06-15T14:10:00+08:00",
            self.root / "packages",
        )["package"]["package_id"]

        with patch("activity_mvp.memory_ledger.Path.home", return_value=fake_home), patch(
            "activity_mvp.memory_approval.Path.home", return_value=fake_home
        ):
            candidates = memory_approval_candidates(self.conn, package_id=package)
            self.assertGreaterEqual(candidates["eligible_candidate_count"], 1)
            candidate_key = candidates["memory_candidates"][0]["candidate_key"]
            preview = preview_memory_approval(
                self.conn,
                action="approve-candidate",
                package_id=package,
                candidate_key=candidate_key,
                plan_dir=fake_home / "approvals",
            )
        self.assertEqual(preview["status"], "controlled_mem0_writer_disabled")
        self.assertFalse(preview["destructive_actions"])
        self.assertNotIn("plan_id", preview)
        self.assertFalse(log_path.exists())
        audit = self.conn.execute(
            "SELECT COUNT(*) AS count FROM audit_logs WHERE action = 'memory_approval_candidate_write'"
        ).fetchone()["count"]
        self.assertEqual(audit, 0)


    def test_memory_approval_review_only_has_no_write_action_while_writer_disabled(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-15T15:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Personal AI Monitor notes",
                    "ocr_text": "Local Owner preference: compact memory approval panes are easier to scan during Personal AI Monitor work.",
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-15T15:05:00+08:00",
                    "display_id": "main",
                    "app_name": "Google Chrome",
                    "window_title": "Resume portfolio - Chrome",
                    "ocr_text": "Resume portfolio education work history leadership operations interview notes",
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Local Owner preference: compact memory approval panes are easier to scan during Personal AI Monitor work.",
            "2026-06-15T15:00:10+08:00",
        )
        package = generate_package(
            self.conn,
            "2026-06-15T14:59:00+08:00",
            "2026-06-15T15:12:00+08:00",
            self.root / "packages",
        )["package"]["package_id"]

        candidates = memory_approval_candidates(self.conn, package_id=package, limit=40)
        review_items = candidates["review_only_candidates"]
        self.assertGreaterEqual(len(review_items), 1)
        weak = next(item for item in review_items if "single_evidence_demoted" in item["quality_flags"])
        self.assertFalse(weak["can_approve"])
        self.assertFalse(weak["can_promote"])
        self.assertEqual(weak["approval_state"], "blocked")
        missing_edit = preview_memory_approval(
            self.conn,
            action="promote-review-only",
            package_id=package,
            candidate_key=weak["candidate_key"],
            memory_text=weak["safe_summary"],
            plan_dir=self.root / "approval-review",
        )
        self.assertEqual(missing_edit["status"], "controlled_mem0_writer_disabled")
        self.assertFalse(missing_edit["destructive_actions"])
        self.assertNotIn("plan_id", missing_edit)


    def test_decision_queue_combines_memory_candidates_and_review_signals(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-15T16:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Personal AI Monitor decision queue",
                    "ocr_text": "Decision: Local Owner confirms Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-15T16:04:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Personal AI Monitor decision queue",
                    "ocr_text": "Decision confirmed by Local Owner: Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Decision: Local Owner confirms Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
            "2026-06-15T16:00:10+08:00",
        )
        self._manual_confirm(
            "Decision confirmed by Local Owner: Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
            "2026-06-15T16:04:10+08:00",
        )
        package = generate_package(
            self.conn,
            "2026-06-15T15:58:00+08:00",
            "2026-06-15T16:08:00+08:00",
            self.root / "packages",
        )["package"]["package_id"]

        queue = decision_queue_list(
            self.conn,
            range_name="today",
            start="2026-06-15T15:58:00+08:00",
            end="2026-06-15T16:08:00+08:00",
            package_id=package,
            limit=80,
        )

        self.assertEqual(queue["status"], "ok")
        self.assertFalse(queue["raw_access"])
        self.assertFalse(queue["destructive_actions"])
        self.assertFalse(queue["memory_write_actions"])
        self.assertGreaterEqual(queue["memory_candidate_count"], 1)
        self.assertGreaterEqual(queue["review_signal_count"], 1)
        self.assertTrue(any(item["source_kind"] == "memory_candidate" for item in queue["items"]))
        self.assertTrue(any(item["source_kind"].startswith("ai_review_") for item in queue["items"]))
        self.assertTrue(all(not item["can_direct_write"] for item in queue["items"]))
        self.assertTrue(all(not item["can_rewrite_write"] for item in queue["items"]))
        self.assertTrue(all("direct-write" not in item["available_actions"] for item in queue["items"]))
        self.assertTrue(all("rewrite-write" not in item["available_actions"] for item in queue["items"]))


    def test_decision_queue_review_signal_must_be_rewritten_and_blocks_unsafe_text(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-15T16:30:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "decision",
                    "ocr_text": "Decision: add a left-sidebar decision queue for uncertain Personal AI Monitor signals.",
                    "context_confidence": 0.9,
                }
            ],
        )
        item = next(
            item
            for item in decision_queue_list(
                self.conn,
                range_name="today",
                start="2026-06-15T16:28:00+08:00",
                end="2026-06-15T16:35:00+08:00",
                limit=80,
            )["items"]
            if item["source_kind"].startswith("ai_review_")
        )

        missing_edit = preview_decision_queue_write(
            self.conn,
            item_key=item["item_key"],
            memory_text=item["safe_summary"],
            start="2026-06-15T16:28:00+08:00",
            end="2026-06-15T16:35:00+08:00",
            plan_dir=self.root / "decision-plans",
        )
        unsafe = preview_decision_queue_write(
            self.conn,
            item_key=item["item_key"],
            memory_text="Local Owner 的安全摘要来自 " + "/Users/" + "example/private/raw.txt",
            start="2026-06-15T16:28:00+08:00",
            end="2026-06-15T16:35:00+08:00",
            plan_dir=self.root / "decision-plans",
        )
        preview = preview_decision_queue_write(
            self.conn,
            item_key=item["item_key"],
            memory_text="Local Owner 希望 Personal AI Monitor 为不确定线索提供左侧待决策入口。",
            start="2026-06-15T16:28:00+08:00",
            end="2026-06-15T16:35:00+08:00",
            plan_dir=self.root / "decision-plans",
        )

        self.assertEqual(missing_edit["status"], "review_signal_not_self_owned")
        self.assertEqual(unsafe["status"], "review_signal_not_self_owned")
        self.assertEqual(preview["status"], "review_signal_not_self_owned")
        self.assertNotIn("plan_id", missing_edit)
        self.assertNotIn("plan_id", unsafe)
        self.assertNotIn("plan_id", preview)
        self.assertNotIn("/Users/example", json.dumps(unsafe, ensure_ascii=False))


    def test_decision_queue_never_write_rule_removes_similar_candidate_from_write_input(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-15T17:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Personal AI Monitor decision queue",
                    "ocr_text": "Decision: Local Owner confirms Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-15T17:03:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Personal AI Monitor decision queue",
                    "ocr_text": "Decision confirmed by Local Owner: Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Decision: Local Owner confirms Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
            "2026-06-15T17:00:10+08:00",
        )
        self._manual_confirm(
            "Decision confirmed by Local Owner: Personal AI Monitor v0.3.2 uses a user decision queue for safe candidate review.",
            "2026-06-15T17:03:10+08:00",
        )
        package = generate_package(
            self.conn,
            "2026-06-15T16:58:00+08:00",
            "2026-06-15T17:08:00+08:00",
            self.root / "packages",
        )["package"]["package_id"]
        before = get_package_memory_candidates(self.conn, package, max_items=40)
        self.assertGreaterEqual(before["memory_candidate_count"], 1)
        queue_item = next(item for item in decision_queue_list(self.conn, package_id=package)["items"] if item["source_kind"] == "memory_candidate")

        recorded = record_decision_queue_action(
            self.conn,
            item_key=queue_item["item_key"],
            action="never-write",
            generalize_scope="similar",
            package_id=package,
        )
        after = get_package_memory_candidates(self.conn, package, max_items=40)

        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(after["memory_candidate_count"], before["memory_candidate_count"] - 1)
        self.assertGreaterEqual(after["review_only_candidate_count"], before["review_only_candidate_count"] + 1)
        self.assertEqual(after["memory_candidate_skip_reason"], "user_decision_suppressed")
        self.assertGreaterEqual(after["user_decision_suppressed_count"], 1)


    def test_decision_queue_feedback_actions_map_to_local_rules_without_write(self) -> None:
        candidates = {
            "status": "ok",
            "package_id": "pkg_feedback",
            "memory_candidates": [],
            "review_only_candidates": [
                {
                    "candidate_key": "cand_folder_feedback",
                    "safe_summary": "Finder 侧边栏里出现 NVIDIA 项目经理文件夹名。",
                    "topic": "文件夹误判",
                    "app": "Finder",
                    "time_label": "今天",
                    "candidate_type": "activity_fact",
                    "confidence": 0.66,
                    "why_keep": "疑似侧边栏或文件列表。",
                    "evidence_count": 1,
                    "source_category": "general_document",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "review_only",
                    "quality_flags": ["screen_region_noise"],
                    "screen_region_signal": {
                        "region_kind": "sidebar",
                        "actionability": "noise",
                        "source": "rule",
                        "confidence": 0.86,
                        "reason_codes": ["finder_sidebar"],
                    },
                    "can_approve": False,
                    "can_promote": True,
                    "disabled_reason": "需要人工改写。",
                },
                {
                    "candidate_key": "cand_correct_feedback",
                    "safe_summary": "Local Owner 正在 ChatGPT 中多轮确认以色列市场调研方向。",
                    "topic": "以色列市场调研",
                    "app": "ChatGPT",
                    "time_label": "今天",
                    "candidate_type": "project_fact",
                    "confidence": 0.88,
                    "why_keep": "多轮可信上下文。",
                    "evidence_count": 2,
                    "source_category": "general_document",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "eligible",
                    "quality_flags": ["durable_candidate"],
                    "screen_region_signal": {
                        "region_kind": "chat_thread",
                        "actionability": "actionable",
                        "source": "rule",
                        "confidence": 0.8,
                        "reason_codes": ["chat_app"],
                    },
                    "can_approve": True,
                    "can_promote": False,
                    "disabled_reason": "",
                },
            ],
            "ephemeral_candidates": [],
        }
        with patch("activity_mvp.decision_queue.memory_approval_candidates", return_value=candidates), patch(
            "activity_mvp.decision_queue.build_ai_handoff_package",
            return_value={"status": "ok", "decision_signals": [], "follow_ups": [], "blockers": []},
        ):
            queue = decision_queue_list(self.conn, package_id="pkg_feedback", limit=20)
            folder = record_decision_queue_action(
                self.conn,
                item_key="cand_folder_feedback",
                action="folder-sidebar",
                package_id="pkg_feedback",
            )
            correct = record_decision_queue_action(
                self.conn,
                item_key="cand_correct_feedback",
                action="correct",
                package_id="pkg_feedback",
            )
            decided = decision_queue_list(self.conn, package_id="pkg_feedback", include_decided=True, limit=20)

        self.assertEqual(queue["feedback_actions"][0]["action"], "folder-sidebar")
        self.assertIn("folder-sidebar", queue["items"][0]["available_actions"])
        self.assertEqual(folder["status"], "recorded")
        self.assertEqual(folder["requested_action"], "folder-sidebar")
        self.assertEqual(folder["recorded_action"], "never-write")
        self.assertFalse(folder["memory_write_actions"])
        self.assertEqual(correct["status"], "recorded")
        self.assertEqual(correct["recorded_action"], "correct-signal")
        self.assertFalse(correct["memory_write_actions"])
        by_key = {item["item_key"]: item for item in decided["items"]}
        self.assertEqual(by_key["cand_folder_feedback"]["decision_state"], "user_never_write")
        self.assertEqual(by_key["cand_correct_feedback"]["decision_state"], "user_marked_correct")


    def test_decision_queue_exposes_only_non_write_actions_while_writer_disabled(self) -> None:
        candidates = {
            "status": "ok",
            "package_id": "pkg_question",
            "memory_candidates": [
                {
                    "candidate_key": "cand_eligible_question",
                    "safe_summary": "Local Owner 希望 PAM 待决策页用中文问题卡表达。",
                    "topic": "待决策问题卡",
                    "app": "Codex",
                    "time_label": "今天",
                    "candidate_type": "project_fact",
                    "confidence": 0.91,
                    "why_keep": "这是稳定的产品交互偏好。",
                    "evidence_count": 2,
                    "source_category": "self_project_fact",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "eligible",
                    "quality_flags": ["durable_candidate"],
                    "can_approve": True,
                    "can_promote": False,
                    "disabled_reason": "",
                }
            ],
            "review_only_candidates": [
                {
                    "candidate_key": "cand_review_question",
                    "safe_summary": "PAM 待决策页需要用户改写确认。",
                    "topic": "改写确认交互",
                    "app": "Codex",
                    "time_label": "今天",
                    "candidate_type": "interaction_preference",
                    "confidence": 0.68,
                    "why_keep": "这条还不像一句可长期保存的事实。",
                    "evidence_count": 1,
                    "source_category": "self_project_fact",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "review_only",
                    "quality_flags": ["single_evidence_demoted"],
                    "can_approve": False,
                    "can_promote": True,
                    "disabled_reason": "需要人工改写。",
                }
            ],
            "ephemeral_candidates": [
                {
                    "candidate_key": "cand_ephemeral_question",
                    "safe_summary": "PAM 当次页面状态只适合临时参考。",
                    "topic": "临时页面状态",
                    "app": "Personal AI Monitor",
                    "time_label": "今天",
                    "candidate_type": "activity_fact",
                    "confidence": 0.55,
                    "why_keep": "这是当次运行状态。",
                    "evidence_count": 1,
                    "source_category": "temporary_ui_state",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "ephemeral",
                    "quality_flags": ["ephemeral"],
                    "can_approve": False,
                    "can_promote": False,
                    "disabled_reason": "",
                }
            ],
        }
        with patch("activity_mvp.decision_queue.memory_approval_candidates", return_value=candidates), patch(
            "activity_mvp.decision_queue.build_ai_handoff_package",
            return_value={"status": "ok", "decision_signals": [], "follow_ups": [], "blockers": []},
        ), patch("activity_mvp.decision_queue._duplicate_report", return_value={"exact_duplicate": False, "similar_duplicate_count": 0}):
            queue = decision_queue_list(self.conn, package_id="pkg_question", limit=20)

        self.assertEqual(queue["status"], "ok")
        self.assertFalse(queue["raw_access"])
        self.assertFalse(queue["memory_write_actions"])
        self.assertEqual(queue["auto_handled_count"], 0)
        by_key = {item["item_key"]: item for item in queue["items"]}

        eligible = by_key["cand_eligible_question"]
        self.assertEqual(eligible["decision_question"], "长期记忆写入已关闭；这条只作当前参考，要标为参考还是永不写入？")
        self.assertEqual(eligible["candidate_sentence"], "Local Owner 希望 PAM 待决策页用中文问题卡表达。")
        self.assertNotIn("direct-write", eligible["available_actions"])
        self.assertNotIn("rewrite-write", eligible["available_actions"])
        self.assertFalse(eligible["can_direct_write"])
        self.assertFalse(eligible["can_rewrite_write"])
        self.assertEqual(eligible["recommended_action"], "context-only")
        self.assertEqual(eligible["queue_bucket"], "context_only")

        review = by_key["cand_review_question"]
        self.assertNotIn("direct-write", review["available_actions"])
        self.assertNotIn("rewrite-write", review["available_actions"])
        self.assertFalse(review["can_direct_write"])
        self.assertFalse(review["can_rewrite_write"])
        self.assertEqual(review["recommended_action"], "context-only")
        self.assertEqual(review["queue_bucket"], "context_only")

        ephemeral = by_key["cand_ephemeral_question"]
        self.assertNotIn("direct-write", ephemeral["available_actions"])
        self.assertNotIn("rewrite-write", ephemeral["available_actions"])
        self.assertIn("context-only", ephemeral["available_actions"])
        self.assertIn("never-write", ephemeral["available_actions"])
        self.assertEqual(ephemeral["recommended_action"], "context-only")
        self.assertEqual(ephemeral["queue_bucket"], "context_only")


    def test_decision_queue_filters_noise_hard_blocks_and_merges_duplicates(self) -> None:
        candidates = {
            "status": "ok",
            "package_id": "pkg_filter",
            "memory_candidates": [],
            "review_only_candidates": [
                {
                    "candidate_key": "cand_merge_a",
                    "safe_summary": "PAM 待决策页需要让用户看懂系统识别到的内容。",
                    "topic": "待决策可读性",
                    "app": "Codex",
                    "candidate_type": "interaction_preference",
                    "confidence": 0.71,
                    "why_keep": "需要人工复核。",
                    "evidence_count": 1,
                    "source_category": "self_project_fact",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "review_only",
                    "quality_flags": ["single_evidence_demoted"],
                    "can_approve": False,
                    "can_promote": True,
                    "disabled_reason": "需要人工改写。",
                },
                {
                    "candidate_key": "cand_merge_b",
                    "safe_summary": "PAM 待决策页需要把不确定原因说清楚。",
                    "topic": "待决策可读性",
                    "app": "Codex",
                    "candidate_type": "interaction_preference",
                    "confidence": 0.73,
                    "why_keep": "同类候选。",
                    "evidence_count": 3,
                    "source_category": "self_project_fact",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "review_only",
                    "quality_flags": ["single_evidence_demoted"],
                    "can_approve": False,
                    "can_promote": True,
                    "disabled_reason": "需要人工改写。",
                },
                {
                    "candidate_key": "cand_ui_noise",
                    "safe_summary": "Personal AI Monitor 待决策 页面显示 raw_access=false、重新读取、先预览、不自动写。",
                    "topic": "Personal AI Monitor 待决策",
                    "app": "Personal AI Monitor",
                    "candidate_type": "activity_fact",
                    "confidence": 0.80,
                    "why_keep": "页面 OCR 噪声。",
                    "evidence_count": 1,
                    "source_category": "self_project_fact",
                    "subject_scope": "user_workflow_only",
                    "write_eligibility": "review_only",
                    "quality_flags": [],
                    "can_approve": False,
                    "can_promote": True,
                    "disabled_reason": "需要人工改写。",
                },
                {
                    "candidate_key": "cand_hard_block",
                    "safe_summary": "参考岗位 JD 中出现的一段内容。",
                    "topic": "岗位 JD",
                    "app": "Chrome",
                    "candidate_type": "job_document",
                    "confidence": 0.90,
                    "why_keep": "硬阻断。",
                    "evidence_count": 2,
                    "source_category": "target_role_jd",
                    "subject_scope": "ambiguous_subject",
                    "write_eligibility": "review_only",
                    "quality_flags": ["reference_only_document"],
                    "can_approve": False,
                    "can_promote": True,
                    "disabled_reason": "硬阻断：target_role_jd 来源类别不可写。",
                },
            ],
            "ephemeral_candidates": [],
        }
        with patch("activity_mvp.decision_queue.memory_approval_candidates", return_value=candidates), patch(
            "activity_mvp.decision_queue.build_ai_handoff_package",
            return_value={"status": "ok", "decision_signals": [], "follow_ups": [], "blockers": []},
        ), patch("activity_mvp.decision_queue._duplicate_report", return_value={"exact_duplicate": False, "similar_duplicate_count": 0}):
            queue = decision_queue_list(self.conn, package_id="pkg_filter", limit=20)

        self.assertEqual(queue["item_count"], 1)
        item = queue["items"][0]
        self.assertEqual(item["item_key"], "cand_merge_a")
        self.assertEqual(item["merged_item_count"], 2)
        self.assertEqual(item["evidence_count"], 3)

        auto = {entry["reason"]: entry["count"] for entry in queue["auto_handled"]}
        self.assertEqual(auto["self_ui_noise"], 1)
        self.assertEqual(auto["hard_blocked"], 1)
        self.assertEqual(auto["merged_duplicate"], 1)
        self.assertEqual(queue["auto_handled_count"], 3)


    def test_local_endpoint_guard_allows_only_loopback_http(self) -> None:
        for endpoint in [
            "http://127.0.0.1:11434/api/generate",
            "http://localhost:11434/api/generate",
            "http://[::1]:11434/api/generate",
        ]:
            self.assertTrue(validate_local_http_endpoint(endpoint)["allowed"], endpoint)

        for endpoint in [
            "https://example.com/api/generate",
            "http://192.168.1.50:11434/api/generate",
            "file:///tmp/model.sock",
            "",
        ]:
            self.assertFalse(validate_local_http_endpoint(endpoint)["allowed"], endpoint)


    def test_configure_privacy_profile_updates_status_with_personal_default(self) -> None:
        result = configure_privacy_profile(self.conn, self.root, profile="personal_high_precision")
        self.assertEqual(result["profile"], "personal_high_precision")
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)
        self.assertEqual(status["privacy_profile"]["configured"], "personal_high_precision")
        self.assertEqual(status["privacy_profile"]["source_default"], "personal_high_precision")
        self.assertTrue(status["privacy_profile"]["personal_high_precision"])


    def test_privacy_policy_status_and_preview_are_read_only(self) -> None:
        status = privacy_policy_status(self.conn, self.root)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["mode"], "read_only_preview")
        self.assertFalse(status["raw_access"])
        self.assertFalse(status["destructive_actions"])
        self.assertFalse(status["rule_write_actions"])
        self.assertGreater(status["rule_counts"]["hard_block_apps"], 0)
        live_like_root = self.root / "Library" / "Application Support" / "Personal AI Monitor" / "core" / "data"
        live_like_root.mkdir(parents=True)
        live_like_status = privacy_policy_status(self.conn, live_like_root)
        self.assertEqual(live_like_status["runtime_root"], "Application Support live runtime")

        before_settings = self.conn.execute("SELECT COUNT(*) AS count FROM settings").fetchone()["count"]
        preview = privacy_policy_preview(self.conn, app="Safari", domain="paypal.example", title="Checkout")
        after_settings = self.conn.execute("SELECT COUNT(*) AS count FROM settings").fetchone()["count"]

        self.assertEqual(before_settings, after_settings)
        self.assertEqual(preview["status"], "ok")
        self.assertFalse(preview["raw_access"])
        self.assertFalse(preview["rule_write_actions"])
        self.assertFalse(preview["ai_handoff_effect"]["readable_detail"])
        self.assertTrue(preview["ai_handoff_effect"]["count_only"])
        self.assertIn("url_blacklist", preview["classification"]["reason"])

        profile_preview = privacy_profile_preview(self.conn, self.root, profile="strict")
        settings_after_profile_preview = self.conn.execute("SELECT COUNT(*) AS count FROM settings").fetchone()["count"]
        self.assertEqual(after_settings, settings_after_profile_preview)
        self.assertEqual(profile_preview["mode"], "profile_dry_run")
        self.assertEqual(profile_preview["target_profile"], "strict")
        self.assertTrue(profile_preview["requires_confirmation"])
        self.assertFalse(profile_preview["rule_write_actions"])
        self.assertFalse(profile_preview["changes_runtime_policy"])
        self.assertIn("personal_clinical_medical_content", profile_preview["unchanged_boundaries"])


    def test_daily_review_uses_clear_activity_only(self) -> None:
        ingest_events(self.conn, EVENTS)

        review = daily_review(
            self.conn,
            "2026-05-22T08:59:00+08:00",
            "2026-05-22T09:12:00+08:00",
        )

        self.assertEqual(review["source"], "local_owner_observation_v1")
        self.assertEqual(review["cloud_analysis"], "off")
        self.assertEqual(review["activity_count"], 1)
        encoded = json.dumps(review, ensure_ascii=False)
        self.assertIn("Personal AI Monitor", encoded)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", encoded)
        self.assertNotIn("review_required", encoded)


    def test_ai_handoff_package_exposes_safe_clear_details_only(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T15:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "PAM v0.1.19",
                    "ocr_text": "Decision: v0.1.19 adds a read-only AI handoff package and needs tests.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T15:02:00+08:00",
                    "display_id": "main",
                    "app_name": "Typora",
                    "window_title": "partial",
                    "ocr_text": "PARTIAL_DETAIL_SHOULD_NOT_ENTER_AI_HANDOFF safe text.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T15:04:00+08:00",
                    "display_id": "main",
                    "app_name": "Feishu",
                    "window_title": "客户A",
                    "ocr_text": "客户A 私聊正文 需要明天回复",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T15:06:00+08:00",
                    "display_id": "external",
                    "app_name": "1Password",
                    "window_title": "Secure Note",
                    "ocr_text": "secret REDACTED_TEST_CREDENTIAL_VALUE_DO_NOT_STORE",
                    "secure_input": True,
                    "context_confidence": 0.9,
                },
            ],
        )
        self.conn.execute(
            """
            UPDATE extracted_activities
            SET privacy_level = 'partial_redacted'
            WHERE activity_summary LIKE '%PARTIAL_DETAIL_SHOULD_NOT_ENTER_AI_HANDOFF%'
            """
        )
        self.conn.commit()
        self._seed_historical_confirmed_business_contexts(
            "2026-05-22T14:59:00+08:00",
            "2026-05-22T15:08:00+08:00",
            workstream="客户沟通",
            outcome="progressing",
        )
        manual_add_confirmed_activity(
            self.conn,
            confirmation_action=MANUAL_CONFIRMATION_ACTION,
            safe_label="Decision: v0.1.19 adds a read-only AI handoff package and needs tests.",
            workstream="PAM engineering",
            outcome="progressing",
            started_at="2026-05-22T15:00:30+08:00",
            ended_at="2026-05-22T15:00:31+08:00",
        )

        package = build_ai_handoff_package(
            self.conn,
            start="2026-05-22T14:59:00+08:00",
            end="2026-05-22T15:08:00+08:00",
        )
        encoded = json.dumps(package, ensure_ascii=False)

        self.assertEqual(package["status"], "ok")
        self.assertEqual(package["source"], "local_confirmed_activity_only")
        self.assertFalse(package["raw_access"])
        self.assertFalse(package["destructive_actions"])
        self.assertFalse(package["memory_write_actions"])
        self.assertEqual(package["summary"]["activity_count"], 2)
        self.assertEqual(package["summary"]["readable_activity_count"], 1)
        self.assertEqual(package["summary"]["partial_redacted_count"], 0)
        self.assertEqual(package["summary"]["communication_redacted_count"], 1)
        self.assertEqual(package["unverified_clue_count"], 2)
        self.assertEqual(package["summary"]["unverified_clue_count"], 2)
        self.assertGreaterEqual(len(package["decision_signals"]), 1)
        self.assertIn("read-only AI handoff", encoded)
        self.assertNotIn("PARTIAL_DETAIL_SHOULD_NOT_ENTER_AI_HANDOFF", encoded)
        self.assertNotIn("客户A", encoded)
        self.assertNotIn("私聊正文", encoded)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", encoded)
        self.assertNotIn("ocr_text", encoded)
        self.assertNotIn("cleaned_text", encoded)
        self.assertNotIn("event_id", encoded)
        self.assertNotIn("memory_id", encoded)


    def test_ai_handoff_markdown_and_export_block_sensitive_residue(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T16:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "handoff",
                    "ocr_text": "Implement AI handoff export and verify JSON and Markdown package output.",
                    "context_confidence": 0.9,
                }
            ],
        )
        manual_add_confirmed_activity(
            self.conn,
            confirmation_action=MANUAL_CONFIRMATION_ACTION,
            safe_label="Implement AI handoff export and verify JSON and Markdown package output.",
            workstream="PAM engineering",
            outcome="progressing",
            started_at="2026-05-22T16:00:30+08:00",
            ended_at="2026-05-22T16:00:31+08:00",
        )
        package = build_ai_handoff_package(
            self.conn,
            start="2026-05-22T15:59:00+08:00",
            end="2026-05-22T16:02:00+08:00",
        )
        markdown = render_ai_handoff_markdown(package)

        self.assertIn("# PAM AI 接手包", markdown)
        self.assertIn("raw_access: `false`", markdown)
        self.assertIn("Implement AI handoff export", markdown)
        self.assertNotIn("ocr_text", markdown)
        self.assertNotIn("cleaned_text", markdown)

        output = self.root / "exports" / "handoff.md"
        result = write_ai_handoff_export(output, markdown, package=package, output_format="markdown")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["markdown_path"], str(output.resolve()))
        self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")

        bad_package = json.loads(json.dumps(package))
        sensitive_path = "/Users/" + "private/Documents/raw_vault/frame.png"
        bad_package["safe_activity_samples"] = [
            {
                "safe_summary": sensitive_path,
            }
        ]
        bad_markdown = render_ai_handoff_markdown(bad_package)
        with self.assertRaises(ValueError):
            write_ai_handoff_export(self.root / "exports" / "bad.md", bad_markdown, package=bad_package, output_format="markdown")


    def test_ai_handoff_privacy_scan_blocks_confirmation_evidence_private_fields(self) -> None:
        private_fields = (
            "clarification_note",
            "evidence_id",
            "source_frame_id",
            "content_sha256",
            "confirmation_evidence",
            "confirmation_evidence_files",
            "confirmation_evidence_refs",
            "file_name",
            "source_activity_id",
            "source_activity_ids",
            "deletion_group_id",
            "deletion_group_ids",
            "invalid_evidence_id",
            "invalid_evidence_ids",
            "invalid_file_name",
            "invalid_file_names",
        )
        for field in private_fields:
            with self.subTest(field=field):
                scan = validate_ai_handoff_payload({field: "PRIVATE_CONFIRMATION_EVIDENCE"})
                self.assertEqual(scan["status"], "blocked")
                self.assertIn("raw_or_internal_field", scan["findings"])

        public_fields = {
            "evidence_status": "available",
            "evidence_count": 1,
            "available_source_count": 1,
            "file_name_redacted": "README.md",
            "confirmation_evidence_health": {"status": "ok", "raw_access": False},
        }
        self.assertTrue(validate_ai_handoff_payload(public_fields)["ok"])


    def test_timeline_markdown_export_includes_cleaned_event_types(self) -> None:
        ingest_events(self.conn, EVENTS)

        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T00:59:00Z",
            "2026-05-22T01:12:00Z",
        )
        markdown = render_cleaned_timeline_markdown(export)
        expected_start = parse_time("2026-05-22T00:59:00Z").astimezone().isoformat(timespec="seconds")
        expected_event_time = parse_time("2026-05-22T09:00:00+08:00").astimezone().isoformat(timespec="seconds")

        self.assertIn("# 清洗后完整时间线", markdown)
        self.assertIn(f"time_range: `{expected_start}`", markdown)
        self.assertIn(expected_event_time, markdown)
        self.assertIn("clear_activity_count: `1`", markdown)
        self.assertIn("privacy_event_count: `1`", markdown)
        self.assertIn("gap_event_count: `1`", markdown)
        self.assertIn("total_event_count: `3`", markdown)
        self.assertIn("raw_access: `false`", markdown)
        self.assertIn("cloud_analysis: `off`", markdown)
        self.assertIn("清洗后完整内容", markdown)
        self.assertIn("Personal AI Monitor", markdown)
        self.assertIn("隐私保护", markdown)
        self.assertIn("未记录缺口", markdown)
        self.assertNotIn("无活动摘要", markdown)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", markdown)
        self.assertNotIn("review_required", markdown)
        export_path = write_timeline_markdown(self.root / "exports" / "timeline.md", markdown)
        self.assertTrue(export_path.exists())
        self.assertEqual(export_path.read_text(encoding="utf-8"), markdown)


    def test_unconfirmed_screen_content_stays_out_of_trusted_timeline_and_ha_package(self) -> None:
        tail = "TAIL_MARKER_FOR_CLEANED_EXPORT"
        long_text = " ".join(["普通安全内容"] * 80 + [tail])
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:40:00+08:00",
                    "display_id": "main",
                    "app_name": "Typora",
                    "window_title": "长文草稿",
                    "ocr_text": long_text,
                    "context_confidence": 0.9,
                }
            ],
        )

        # The local ingestion record still contains privacy-cleaned text, but a
        # screen/OCR observation alone must not become trusted export content.
        activity = timeline(self.conn)[0]
        self.assertIn(tail, activity["cleaned_text"])
        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T10:39:00+08:00",
            "2026-05-22T10:41:00+08:00",
        )
        self.assertEqual(len(export["activities"]), 1)
        self.assertEqual(export["trusted_activities"], [])
        self.assertEqual(export["activities"][0]["verification_state"], "unverified")
        self.assertEqual(export["unverified_clue_count"], 1)
        self.assertEqual(len(export["unverified_clues"]), 1)
        self.assertNotIn("cleaned_text", export["unverified_clues"][0])
        markdown = render_cleaned_timeline_markdown(export)
        self.assertIn("可信导出", markdown)
        self.assertIn(tail, markdown)

        package = generate_package(
            self.conn,
            "2026-05-22T10:39:00+08:00",
            "2026-05-22T10:41:00+08:00",
            self.root / "packages",
        )["package"]
        encoded = json.dumps(package, ensure_ascii=False)
        self.assertNotIn(tail, encoded)
        self.assertIn("unverified_screen_or_ocr_clue", encoded)


    def test_learning_timeline_classifies_safe_content_without_private_detail(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T13:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "debug tests",
                    "ocr_text": "Fix python unittest failure in timeline_export and rerun tests for the package output.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:00:15+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "debug tests",
                    "ocr_text": "Fix python unittest failure in timeline_export and rerun tests for the package output.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:02:00+08:00",
                    "display_id": "main",
                    "app_name": "Typora",
                    "window_title": "公众号草稿",
                    "ocr_text": "撰写 Personal AI Monitor 文章草稿，调整标题、段落和 Markdown 排版。",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:04:00+08:00",
                    "display_id": "main",
                    "app_name": "Dia",
                    "window_title": "research",
                    "ocr_text": "Research local-first privacy design and compare competitor documentation.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:06:00+08:00",
                    "display_id": "main",
                    "app_name": "Finder",
                    "window_title": "Downloads",
                    "ocr_text": "整理 /Users/example/Downloads/project 文件和归档目录。",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:08:00+08:00",
                    "display_id": "main",
                    "app_name": "Feishu",
                    "window_title": "客户A",
                    "ocr_text": "客户A 私聊正文 需要明天回复",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:10:00+08:00",
                    "display_id": "external",
                    "app_name": "1Password",
                    "window_title": "Secure Note",
                    "ocr_text": "secret REDACTED_TEST_CREDENTIAL_VALUE_DO_NOT_STORE",
                    "secure_input": True,
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T13:12:00+08:00",
                    "display_id": "external",
                    "app_name": "Unknown",
                    "window_title": "",
                    "ocr_text": "",
                    "context_confidence": 0.1,
                },
                {
                    "captured_at": "2026-05-22T13:14:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "debug tests",
                    "ocr_text": "Fix python unittest failure in timeline_export and rerun tests for the package output.",
                    "context_confidence": 0.9,
                },
            ],
        )
        self._seed_historical_confirmed_business_contexts(
            "2026-05-22T12:59:00+08:00",
            "2026-05-22T13:16:00+08:00",
            workstream="客户沟通",
            outcome="progressing",
        )
        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T12:59:00+08:00",
            "2026-05-22T13:16:00+08:00",
        )
        markdown = render_learning_timeline_markdown(export)
        digest = export["learning_digest"]

        self.assertIn("# 学习摘要时间线", markdown)
        self.assertIn("coding_debugging", markdown)
        self.assertIn("writing", markdown)
        self.assertIn("research", markdown)
        self.assertIn("file_management", markdown)
        self.assertIn("communication_redacted", markdown)
        self.assertIn("protected", markdown)
        self.assertIn("duplicate", markdown)
        self.assertIn("gap", markdown)
        self.assertIn("[path_redacted]", markdown)
        self.assertNotIn("/Users/", markdown)
        self.assertNotIn("客户A", markdown)
        self.assertNotIn("私聊正文", markdown)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", markdown)
        self.assertGreaterEqual(digest["learnability_counts"]["useful"], 4)
        self.assertGreaterEqual(digest["learnability_counts"]["low_signal"], 1)


    def test_learning_text_model_remote_endpoint_is_rejected_without_request(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T13:20:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "debug tests",
                    "ocr_text": "Fix python unittest failure and rerun package tests.",
                    "context_confidence": 0.9,
                }
            ],
        )
        env = {
            "PAM_LEARNING_TEXT_MODEL": "llama3.1",
            "PAM_LEARNING_TEXT_ENDPOINT": "https://example.com/api/generate",
        }
        with patch.dict(os.environ, env, clear=False), patch("activity_mvp.timeline_export.urllib.request.urlopen") as urlopen:
            export = cleaned_timeline_export(
                self.conn,
                "2026-05-22T13:19:00+08:00",
                "2026-05-22T13:21:00+08:00",
            )

        classifier = export["learning_digest"]["classifier"]["local_text_model"]
        self.assertFalse(classifier["enabled"])
        self.assertEqual(classifier["status"], "endpoint_rejected")
        self.assertEqual(classifier["endpoint_reason"], "non_loopback_host")
        urlopen.assert_not_called()


    def test_ha_package_includes_safe_learning_digest(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T14:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "package learning digest",
                    "ocr_text": "Implement learning_digest for HA Source Package and verify markdown export tests.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T14:02:00+08:00",
                    "display_id": "main",
                    "app_name": "Feishu",
                    "window_title": "客户A",
                    "ocr_text": "客户A 私聊正文",
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Implement learning_digest for HA Source Package and verify markdown export tests.",
            "2026-05-22T14:00:10+08:00",
            workstream="PAM engineering",
        )
        result = generate_package(
            self.conn,
            "2026-05-22T13:59:00+08:00",
            "2026-05-22T14:05:00+08:00",
            self.root / "packages",
        )
        package = result["package"]
        encoded = json.dumps(package, ensure_ascii=False)

        self.assertEqual(package["memory_write_policy"], "ephemeral_only")
        self.assertIn("learning_digest", package)
        self.assertIn("learnability_diagnostics", package)
        self.assertGreaterEqual(package["learning_digest"]["learnability_counts"]["useful"], 1)
        self.assertGreaterEqual(package["learnability_diagnostics"]["learnability_counts"]["useful"], 1)
        self.assertIn("coverage", package["learnability_diagnostics"])
        self.assertIn("quality", package["learnability_diagnostics"])
        self.assertIn("coding_debugging", encoded)
        self.assertEqual(package["learnability_diagnostics"]["quality"]["communication_redaction_count"], 1)
        self.assertIn('"privacy_level": "communication"', encoded)
        self.assertNotIn("客户A", encoded)
        self.assertNotIn("私聊正文", encoded)
        self.assertNotIn("/Users/", encoded)
        validate_package(package)


    def test_status_runtime_fields_do_not_enter_exports_or_ha_package(self) -> None:
        ingest_events(self.conn, EVENTS)
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": True, "pid": "99999", "command": "Python"},
        ):
            status = get_status(self.conn, self.root)
        runtime_path = status["runtime_identity"]["database_path"]
        status_only_terms = [
            "runtime_identity",
            "dashboard_health",
            "capture_freshness",
            "orphan_dashboard_listener",
            "effective_health_status",
            "recent_review_count",
            "cooldown_until",
            "vlm_cooling_down",
            "revision_key",
            "last_canary_status",
            runtime_path,
        ]

        package = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )["package"]
        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
        )
        combined = "\n".join(
            [
                json.dumps(package, ensure_ascii=False),
                render_cleaned_timeline_markdown(export),
                render_learning_timeline_markdown(export),
            ]
        )
        self.assertIn("Personal AI Monitor", combined)
        for term in status_only_terms:
            self.assertNotIn(term, combined)


    def test_ha_automation_generates_active_package_and_runs_hermes_analysis_only(self) -> None:
        ingest_events(self.conn, EVENTS)
        self._manual_confirm(
            "Personal AI Monitor is implementing SQLite FTS and HA Source Package.",
            "2026-05-22T09:00:10+08:00",
        )
        configure_ha_automation(
            self.conn,
            self.root,
            enabled=True,
            interval_minutes=60,
            trigger_mode="direct_hermes",
            hermes_profile="activity-analysis",
        )
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"completed","package_id":"pkg","memories_written_count":2,"skipped_reason":""}',
            stderr="",
        )
        fake_home = self.root / "fake-home-ha-basic"
        fake_home.mkdir()

        with patch("activity_mvp.ha_automation.Path.home", return_value=fake_home), patch(
            "activity_mvp.ha_automation.subprocess.run",
            return_value=completed,
        ) as hermes_run:
            result = run_ha_automation_once(
                self.conn,
                self.root,
                db_path=self.root / "activity.sqlite",
                now=now,
                force=True,
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["triggered"])
        self.assertEqual(result["hermes_result_status"], "analysis_only")
        self.assertEqual(result["memories_written_count"], 0)
        self.assertEqual(result["written_candidate_count"], 0)
        self.assertEqual(result["written_memory_summaries"], [])
        self.assertEqual(result["memory_report"]["status"], "not_required")
        package_id = result["package_id"]
        package = get_package(self.conn, package_id)
        self.assertEqual(package["source_status"], "active")
        self.assertFalse(package["raw_access"])
        self.assertEqual(package["quarantine_status"], "clear_only")
        self.assertEqual(len(package["activities"]), 1)

        row = self.conn.execute("SELECT * FROM ha_automation_jobs WHERE package_id = ?", (package_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["hour_key"], "2026-05-22T01:00:00+00:00")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempt_count"], 1)
        self.assertEqual(row["activity_count"], 1)
        self.assertEqual(row["hermes_result_status"], "analysis_only")
        self.assertEqual(row["memories_written_count"], 0)
        self.assertEqual(row["written_candidate_count"], 0)
        self.assertEqual(row["skipped_candidate_count"], 0)
        self.assertEqual(row["memory_report_status"], "not_required")

        command = hermes_run.call_args_list[0].args[0]
        command_text = "\n".join(command)
        self.assertIn("--profile", command)
        self.assertIn("activity-analysis", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("clarify", command)
        self.assertIn("analysis_input_json", command_text)
        self.assertIn("你没有终端、文件或 memory 工具", command_text)
        self.assertIn("不能写 mem0", command_text)
        self.assertNotIn("get-package", command_text)
        self.assertNotIn("--memory-candidates-only", command_text)
        self.assertIn("source_category=target_role_jd", command_text)
        self.assertIn("reference_only_job_document", command_text)
        self.assertIn("third_party_or_ambiguous_subject", command_text)
        self.assertIn("subject_ownership_unclear", command_text)
        self.assertIn("0 条写入是正确且可接受", command_text)
        self.assertIn("宁可不写也不要写错", command_text)
        self.assertIn(package_id, command_text)
        self.assertNotIn("SQLite FTS", command_text)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", command_text)
        self.assertNotIn(str(self.root / "activity.sqlite"), command_text)
        self.assertEqual(result["memory_ledger"]["status"], "frozen_legacy")
        self.assertTrue(result["memory_ledger"]["read_only"])
        self.assertFalse(result["memory_ledger"]["write_enabled"])

        audit = self.conn.execute(
            """
            SELECT detail
            FROM audit_logs
            WHERE action = 'ha_hourly_package_handoff' AND target_id = ?
            ORDER BY at DESC
            LIMIT 1
            """,
            (package_id,),
        ).fetchone()
        self.assertIsNotNone(audit)
        detail = json.loads(audit["detail"])
        self.assertTrue(detail["handed_to_hermes"])
        self.assertEqual(detail["hermes_profile"], "activity-analysis")
        self.assertEqual(detail["memories_written_count"], 0)
        self.assertEqual(detail["written_candidate_count"], 0)


    def test_ha_automation_package_failure_persists_only_fixed_error_code(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True)
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        secret = "/private/customer/plan.md " + ("b" * 64) + " raw customer sentence"

        with patch("activity_mvp.ha_automation.generate_package", side_effect=RuntimeError(secret)):
            result = run_ha_automation_once(self.conn, self.root, now=now, force=True)

        self.assertEqual(result["error"], "ha_package_generation_failed")
        state = self.conn.execute("SELECT value FROM daemon_state WHERE key = 'ha_auto_last_error'").fetchone()["value"]
        self.assertEqual(state, "ha_package_generation_failed")
        self.assertNotIn(secret, json.dumps(result))


    def test_ha_automation_rejects_unverified_hermes_write_claims(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True, trigger_mode="direct_hermes")
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "completed",
                    "package_id": "pkg",
                    "memories_written_count": 1,
                    "written_candidate_count": 1,
                    "skipped_candidate_count": 3,
                    "written_candidate_types": ["decision"],
                    "skipped_reasons": ["single_evidence_demoted", "media_player_context"],
                    "written_memory_summaries": [
                        "写入 Personal AI Monitor 的 HA 自动包决策摘要，不含候选全文。",
                        f"不要保留 /Users/example/private 或 {SYNTHETIC_SECRET_TOKEN}。",
                    ],
                    "write_audit_summary": "写入 1 条决策，跳过弱锚点和媒体噪声候选。",
                    "skipped_reason": "",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        fake_home = self.root / "fake-home-feishu"
        channel_dir = fake_home / ".hermes" / "profiles" / "feishubot"
        channel_dir.mkdir(parents=True)
        (channel_dir / "channel_directory.json").write_text(
            json.dumps(
                {
                    "platforms": {
                        "feishu": [
                            {"id": "oc_memory", "name": "记忆系统", "type": "group"},
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with patch("activity_mvp.ha_automation.Path.home", return_value=fake_home), patch.dict(
            os.environ,
            {"PAM_MEMORY_REPORT_DRY_RUN": "1"},
        ), patch("activity_mvp.ha_automation.subprocess.run", return_value=completed):
            result = run_ha_automation_once(self.conn, self.root, now=now, force=True)

        self.assertEqual(result["hermes_result_status"], "analysis_only")
        self.assertEqual(result["memories_written_count"], 0)
        self.assertEqual(result["written_candidate_count"], 0)
        self.assertEqual(result["skipped_candidate_count"], 3)
        self.assertEqual(result["written_candidate_types"], [])
        self.assertEqual(result["written_memory_summaries"], [])
        self.assertEqual(result["memory_report"]["status"], "not_required")
        row = self.conn.execute("SELECT * FROM ha_automation_jobs WHERE package_id = ?", (result["package_id"],)).fetchone()
        self.assertEqual(row["hermes_result_status"], "analysis_only")
        self.assertEqual(row["memories_written_count"], 0)
        self.assertEqual(row["written_candidate_count"], 0)
        self.assertEqual(row["skipped_candidate_count"], 3)
        self.assertEqual(json.loads(row["written_candidate_types"]), [])
        self.assertIn("single_evidence_demoted", row["skipped_reasons"])
        self.assertEqual(json.loads(row["written_memory_summaries"]), [])
        self.assertEqual(row["memory_report_status"], "not_required")
        parsed_result = json.loads(row["hermes_result_json"])
        self.assertFalse(parsed_result["write_verified"])
        self.assertEqual(parsed_result["memories_written_count"], 0)
        with patch("activity_mvp.ha_automation.is_application_support_runtime", return_value=True):
            status = ha_automation_status(self.conn, self.root)
        self.assertEqual(status["latest_memories_written_count"], 0)
        self.assertEqual(status["latest_written_candidate_count"], 0)
        self.assertEqual(status["latest_skipped_candidate_count"], 3)
        self.assertEqual(status["latest_written_candidate_types"], [])
        self.assertIn("media_player_context", status["latest_skipped_reasons"])
        self.assertIn("写入 1 条决策", status["latest_write_audit_summary"])
        self.assertEqual(status["latest_memory_report_status"], "not_required")
        self.assertEqual(status["latest_written_memory_summaries"], [])


    def test_memory_write_report_rejects_model_claim_without_mem0_receipt(self) -> None:
        message = _memory_write_report_message(
            package_id="ha_pkg_test",
            hour_key="2026-06-22T12:00:00+00:00",
            hour_start="2026-06-22T12:00:00+00:00",
            hour_end="2026-06-22T13:00:00+00:00",
            hermes_profile="activity-analysis",
            hermes_result={
                "memories_written_count": 2,
                "written_candidate_types": '["workflow", "self_resume"]',
                "skipped_candidate_count": 3,
                "skipped_reasons": '["review_only"]',
                "written_memory_summaries": '["写入一条安全项目事实摘要。"]',
                "write_audit_summary": "写入事件已安全摘要化。",
            },
        )

        self.assertIn("【PAM 分析建议】未核验的模型自报", message)
        self.assertIn("模型自报 2 条写入", message)
        self.assertIn("不代表长期记忆已写入", message)
        self.assertNotIn("写入一条安全项目事实摘要", message)
        self.assertNotIn("package_id", message)
        self.assertNotIn("hour_key", message)
        self.assertNotIn("时间窗", message)
        self.assertNotIn("Hermes profile", message)
        self.assertNotIn("跳过数量", message)
        self.assertNotIn("审计摘要", message)


    def test_ha_automation_does_not_report_unverified_model_write_claim(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True, trigger_mode="direct_hermes")
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "completed",
                    "package_id": "pkg",
                    "memories_written_count": 1,
                    "written_candidate_count": 1,
                    "written_memory_summaries": ["写入一条安全项目事实摘要。"],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        fake_home = self.root / "fake-home-no-feishu"
        fake_home.mkdir()

        with patch("activity_mvp.ha_automation.Path.home", return_value=fake_home), patch(
            "activity_mvp.ha_automation.subprocess.run",
            return_value=completed,
        ):
            result = run_ha_automation_once(self.conn, self.root, now=now, force=True)
            with patch("activity_mvp.ha_automation.is_application_support_runtime", return_value=True):
                status = ha_automation_status(self.conn, self.root)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["triggered"])
        self.assertEqual(result["hermes_result_status"], "analysis_only")
        self.assertEqual(result["memories_written_count"], 0)
        self.assertEqual(result["memory_report"]["status"], "not_required")
        self.assertEqual(status["latest_memory_report_status"], "not_required")
        self.assertEqual(status["memory_report_failed_count"], 0)
        self.assertNotEqual(status["label"], "记忆汇报失败")


    def test_ha_automation_dedupes_hour_key(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True)
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch("activity_mvp.ha_automation.subprocess.run", return_value=completed) as hermes_run:
            first = run_ha_automation_once(self.conn, self.root, now=now, force=True)
            second = run_ha_automation_once(self.conn, self.root, now=now, force=True)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(hermes_run.call_count, 1)
        package_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM distribution_packages WHERE generation_mode = 'hourly_auto'"
        ).fetchone()["count"]
        job_count = self.conn.execute("SELECT COUNT(*) AS count FROM ha_automation_jobs").fetchone()["count"]
        self.assertEqual(package_count, 1)
        self.assertEqual(job_count, 1)


    def test_ha_automation_retries_failed_trigger_without_new_package(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True)
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="temporary hermes failure")
        succeeded = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

        with patch("activity_mvp.ha_automation.subprocess.run", side_effect=[failed, succeeded]):
            first = run_ha_automation_once(self.conn, self.root, now=now, force=True)
            second = run_ha_automation_once(self.conn, self.root, now=now, force=True)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(first["package_id"], second["package_id"])
        package_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM distribution_packages WHERE generation_mode = 'hourly_auto'"
        ).fetchone()["count"]
        row = self.conn.execute("SELECT status, attempt_count FROM ha_automation_jobs").fetchone()
        self.assertEqual(package_count, 1)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempt_count"], 2)


    def test_ha_automation_dead_letters_after_three_failures_and_does_not_block_next_hour(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True)
        first_hour = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        second_hour = datetime(2026, 5, 22, 11, 5, tzinfo=timezone(timedelta(hours=8)))
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="temporary hermes failure")
        succeeded = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"completed","memories_written_count":0,"skipped_reason":"no_write_eligible_memory_candidates"}',
            stderr="",
        )

        with patch("activity_mvp.ha_automation.subprocess.run", side_effect=[failed, failed, failed, succeeded]):
            first = run_ha_automation_once(self.conn, self.root, now=first_hour, force=True)
            second = run_ha_automation_once(self.conn, self.root, now=first_hour, force=True)
            third = run_ha_automation_once(self.conn, self.root, now=first_hour, force=True)
            fourth = run_ha_automation_once(self.conn, self.root, now=second_hour, force=True)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(third["status"], "dead_letter")
        self.assertEqual(fourth["status"], "completed")
        rows = self.conn.execute(
            "SELECT hour_key, status, attempt_count FROM ha_automation_jobs ORDER BY hour_key"
        ).fetchall()
        self.assertEqual(rows[0]["status"], "dead_letter")
        self.assertEqual(rows[0]["attempt_count"], 3)
        self.assertEqual(rows[1]["status"], "completed")


    def test_hygiene_strict_health_treats_older_dead_letters_as_historical_after_success(self) -> None:
        configure_ha_automation(self.conn, self.root, enabled=True)
        self._insert_ha_automation_job(
            hour_key="2026-06-13T05:00:00+00:00",
            status="dead_letter",
            updated_at="2026-06-13T06:45:30+00:00",
            attempt_count=3,
            last_error="hermes auth missing",
            dead_letter_reason="hermes auth missing",
        )
        self._insert_ha_automation_job(
            hour_key="2026-06-15T02:00:00+00:00",
            status="completed",
            updated_at="2026-06-15T03:01:23+00:00",
            completed_at="2026-06-15T03:01:23+00:00",
            hermes_result_status="completed",
        )

        cleanup = hygiene_cleanup_report(self.root, self.root / "activity.sqlite")
        health = cleanup["policy"]["strict_cleanup_health"]
        fake_home = self._install_ready_ha_dependencies()
        with patch("activity_mvp.ha_automation.is_application_support_runtime", return_value=True), patch(
            "activity_mvp.ha_automation.Path.home", return_value=fake_home
        ), patch("activity_mvp.ha_automation.shutil.which", return_value=""):
            status = ha_automation_status(self.conn, self.root)

        self.assertTrue(health["ok"])
        self.assertTrue(health["checks"]["ha_jobs_clear"])
        self.assertEqual(health["checks"]["ha_dead_letter_count"], 1)
        self.assertEqual(health["checks"]["ha_archived_dead_letter_count"], 1)
        self.assertEqual(health["checks"]["ha_blocking_dead_letter_count"], 0)
        self.assertEqual(health["checks"]["ha_blocking_job_count"], 0)
        self.assertEqual(health["issues"], [])
        self.assertEqual(status["label"], "正常")
        self.assertEqual(status["historical_dead_letter_count"], 1)
        self.assertEqual(status["unresolved_dead_letter_count"], 0)


    def test_hygiene_strict_health_still_blocks_current_pending_jobs(self) -> None:
        configure_ha_automation(self.conn, self.root, enabled=True)
        self._insert_ha_automation_job(
            hour_key="2026-06-13T05:00:00+00:00",
            status="dead_letter",
            updated_at="2026-06-13T06:45:30+00:00",
            attempt_count=3,
            dead_letter_reason="old auth failure",
        )
        self._insert_ha_automation_job(
            hour_key="2026-06-15T02:00:00+00:00",
            status="completed",
            updated_at="2026-06-15T03:01:23+00:00",
            completed_at="2026-06-15T03:01:23+00:00",
            hermes_result_status="completed",
        )
        self._insert_ha_automation_job(
            hour_key="2026-06-15T03:00:00+00:00",
            status="pending",
            updated_at="2026-06-15T04:01:00+00:00",
        )

        cleanup = hygiene_cleanup_report(self.root, self.root / "activity.sqlite")
        health = cleanup["policy"]["strict_cleanup_health"]

        self.assertFalse(health["ok"])
        self.assertFalse(health["checks"]["ha_jobs_clear"])
        self.assertEqual(health["checks"]["ha_pending_failed_job_count"], 1)
        self.assertEqual(health["checks"]["ha_archived_dead_letter_count"], 1)
        self.assertEqual(health["checks"]["ha_blocking_dead_letter_count"], 0)
        self.assertEqual(health["checks"]["ha_blocking_job_count"], 1)
        self.assertIn("ha_automation_has_pending_or_failed_jobs", health["issues"])


    def test_hygiene_strict_health_blocks_unresolved_dead_letters(self) -> None:
        configure_ha_automation(self.conn, self.root, enabled=True)
        self._insert_ha_automation_job(
            hour_key="2026-06-15T02:00:00+00:00",
            status="completed",
            updated_at="2026-06-15T03:01:23+00:00",
            completed_at="2026-06-15T03:01:23+00:00",
            hermes_result_status="completed",
        )
        self._insert_ha_automation_job(
            hour_key="2026-06-15T03:00:00+00:00",
            status="dead_letter",
            updated_at="2026-06-15T04:45:30+00:00",
            attempt_count=3,
            last_error="latest hermes failure",
            dead_letter_reason="latest hermes failure",
        )

        cleanup = hygiene_cleanup_report(self.root, self.root / "activity.sqlite")
        health = cleanup["policy"]["strict_cleanup_health"]
        with patch("activity_mvp.ha_automation.is_application_support_runtime", return_value=True):
            status = ha_automation_status(self.conn, self.root)

        self.assertFalse(health["ok"])
        self.assertFalse(health["checks"]["ha_jobs_clear"])
        self.assertEqual(health["checks"]["ha_dead_letter_count"], 1)
        self.assertEqual(health["checks"]["ha_archived_dead_letter_count"], 0)
        self.assertEqual(health["checks"]["ha_blocking_dead_letter_count"], 1)
        self.assertEqual(health["checks"]["ha_blocking_job_count"], 1)
        self.assertIn("ha_automation_has_unresolved_dead_letters", health["issues"])
        self.assertEqual(status["label"], "有死信")
        self.assertEqual(status["unresolved_dead_letter_count"], 1)


    def test_ha_automation_completed_with_zero_memories_records_skip_reason(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True)
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"completed","memories_written_count":0,"skipped_reason":"no_write_eligible_memory_candidates"}',
            stderr="",
        )

        with patch("activity_mvp.ha_automation.subprocess.run", return_value=completed):
            result = run_ha_automation_once(self.conn, self.root, now=now, force=True)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["memories_written_count"], 0)
        self.assertEqual(result["skipped_reason"], "no_write_eligible_memory_candidates")
        row = self.conn.execute("SELECT skipped_reason, hermes_result_json FROM ha_automation_jobs").fetchone()
        self.assertEqual(row["skipped_reason"], "no_write_eligible_memory_candidates")
        self.assertIn("no_write_eligible_memory_candidates", row["hermes_result_json"])
        status = ha_automation_status(self.conn, self.root)
        self.assertGreaterEqual(status["latest_candidate_count"], 1)
        self.assertEqual(status["latest_eligible_candidate_count"], 0)
        self.assertEqual(status["latest_review_only_candidate_count"], 0)
        self.assertEqual(status["latest_context_clue_count"], 0)
        self.assertGreaterEqual(status["latest_noise_or_reference_count"], 1)
        self.assertEqual(status["latest_candidate_skip_reason"], "third_party_or_ambiguous_subject")


    def test_ha_automation_skips_repo_local_without_force(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_ha_automation(self.conn, self.root, enabled=True)
        now = datetime(2026, 5, 22, 10, 5, tzinfo=timezone(timedelta(hours=8)))

        with patch("activity_mvp.ha_automation.subprocess.run") as hermes_run:
            result = run_ha_automation_once(self.conn, self.root, now=now)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "repo_local_not_live_runtime")
        hermes_run.assert_not_called()
        package_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM distribution_packages WHERE generation_mode = 'hourly_auto'"
        ).fetchone()["count"]
        self.assertEqual(package_count, 0)


    def test_ha_automation_status_is_in_daemon_status(self) -> None:
        configure_ha_automation(self.conn, self.root, enabled=True)
        direct = ha_automation_status(self.conn, self.root)
        self.assertFalse(direct["runtime_allowed"])
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": True, "pid": "99999", "command": "Python"},
        ):
            status = get_status(self.conn, self.root)
        self.assertIn("ha_automation", status)
        self.assertEqual(status["ha_automation"]["hermes_profile"], "activity-analysis")
        self.assertEqual(status["ha_automation"]["active_package_count"], 0)
        self.assertIn("quality_advice", status)
        self.assertTrue(status["quality_advice"]["enabled"])


    def test_ha_automation_status_normalizes_legacy_triggered_completed_job(self) -> None:
        self.conn.execute(
            """
            INSERT INTO ha_automation_jobs(
              id, hour_key, started_at, ended_at, package_id, activity_count,
              safe_timeline_event_count, trigger_mode, hermes_profile, status,
              attempt_count, last_error, generated_at, triggered_at, completed_at,
              created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_triggered",
                "2026-06-08T09:00:00+00:00",
                "2026-06-08T09:00:00+00:00",
                "2026-06-08T10:00:00+00:00",
                "ha_pkg_legacy",
                0,
                0,
                "direct_hermes",
                "activity-analysis",
                "triggered",
                1,
                "",
                "2026-06-08T10:01:00+00:00",
                "2026-06-08T10:02:00+00:00",
                "2026-06-08T10:03:00+00:00",
                "2026-06-08T10:01:00+00:00",
                "2026-06-12T20:00:00+00:00",
            ),
        )
        self.conn.execute(
            """
            INSERT INTO ha_automation_jobs(
              id, hour_key, started_at, ended_at, package_id, activity_count,
              safe_timeline_event_count, trigger_mode, hermes_profile, status,
              attempt_count, last_error, generated_at, triggered_at, completed_at,
              candidate_count, eligible_candidate_count, written_candidate_count,
              memories_written_count, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "new_completed",
                "2026-06-12T09:00:00+00:00",
                "2026-06-12T09:00:00+00:00",
                "2026-06-12T10:00:00+00:00",
                "ha_pkg_new",
                3,
                3,
                "direct_hermes",
                "activity-analysis",
                "completed",
                1,
                "",
                "2026-06-12T10:01:00+00:00",
                "2026-06-12T10:02:00+00:00",
                "2026-06-12T10:03:00+00:00",
                50,
                14,
                14,
                2,
                "2026-06-12T10:01:00+00:00",
                "2026-06-12T10:03:00+00:00",
            ),
        )
        self.conn.commit()

        status = ha_automation_status(self.conn, self.root)
        row = self.conn.execute(
            "SELECT status, hermes_result_status, updated_at FROM ha_automation_jobs WHERE id = ?",
            ("legacy_triggered",),
        ).fetchone()

        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["hermes_result_status"], "triggered_legacy")
        self.assertEqual(row["updated_at"], "2026-06-12T20:00:00+00:00")
        self.assertEqual(status["pending_count"], 0)
        self.assertEqual(status["latest_job"]["hour_key"], "2026-06-12T09:00:00+00:00")
        self.assertTrue(status["latest_result_summary"].startswith("completed"))
        self.assertEqual(status["latest_candidate_count"], 50)
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": True, "pid": "99999", "command": "Python"},
        ):
            daemon_status = get_status(self.conn, self.root)
        self.assertEqual(daemon_status["quality_health"]["memory_write_health"]["total_candidate_count"], 50)
        trend = daemon_status["quality_health"]["memory_governance_trend"]
        self.assertGreaterEqual(trend["window_job_count"], 2)
        self.assertEqual(trend["totals"]["candidate_count"], 50)
        self.assertEqual(trend["totals"]["eligible_candidate_count"], 14)
        self.assertFalse(trend["raw_access"])


    def test_validate_ha_automation_checks_profile_mem0_and_runtime_without_tokens(self) -> None:
        configure_ha_automation(self.conn, self.root, enabled=True)
        fake_home = self.root / "fake-home"
        live_root = fake_home / "Library" / "Application Support" / "Personal AI Monitor" / "core"
        profile = fake_home / ".hermes" / "profiles" / "activity-analysis"
        feishu_profile = fake_home / ".hermes" / "profiles" / "feishubot"
        hermes = fake_home / "bin" / "hermes"
        mem0 = fake_home / "bin" / "mem0"
        (profile / "plugins" / "mem0_local").mkdir(parents=True)
        feishu_profile.mkdir(parents=True)
        live_root.mkdir(parents=True)
        hermes.parent.mkdir(parents=True)
        hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        mem0.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hermes.chmod(0o755)
        mem0.chmod(0o755)
        (profile / ".env").write_text("MEM0_USER_ID=local-owner\n", encoding="utf-8")
        (profile / "config.yaml").write_text("memory:\n  provider: mem0_local\nplugins:\n  - mem0_local\n", encoding="utf-8")
        (feishu_profile / "channel_directory.json").write_text(
            json.dumps({"platforms": {"feishu": [{"id": "oc_memory", "name": "记忆系统", "type": "group"}]}}, ensure_ascii=False),
            encoding="utf-8",
        )

        with patch("activity_mvp.ha_automation.Path.home", return_value=fake_home), patch(
            "activity_mvp.ha_automation.shutil.which",
            return_value="",
        ):
            result = validate_ha_automation(self.conn, live_root)

        encoded = json.dumps(result, ensure_ascii=False)
        self.assertTrue(result["ready"])
        self.assertEqual(result["mem0"]["user_id"], "local-owner")
        self.assertTrue(result["memory_report"]["target_resolved"])
        self.assertNotIn("sk-", encoded)
        self.assertNotIn("token", encoded.lower())


    def test_quality_advice_uses_safe_wrappers_and_sanitizes_hermes_output(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_quality_advice(self.conn, self.root, enabled=True, profile="activity-quality-analysis")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "completed",
                    "summary": (
                        "建议加强 DocumentSignal，不保存 /Users/example/private 或 "
                        f"{SYNTHETIC_SECRET_TOKEN}。"
                    ),
                    "issues": [{"issue_type": "missed_document_signal", "severity": "medium", "evidence_count": 1}],
                    "recommendations": [{"area": "document_signal", "priority": "high", "proposal": "提高 Obsidian 匹配权重"}],
                    "next_actions": ["让 Codex 增加回归测试"],
                    "raw_path": "/Users/example/private",
                    "api_key": SYNTHETIC_SECRET_TOKEN,
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        with patch("activity_mvp.quality_advice.subprocess.run", return_value=completed) as hermes_run:
            result = run_quality_advice_once(
                self.conn,
                self.root,
                db_path=self.root / "activity.sqlite",
                start="2026-05-22T09:00:00+08:00",
                end="2026-05-22T09:12:00+08:00",
                force=True,
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["triggered"])
        command = hermes_run.call_args.args[0]
        prompt = command[-1]
        self.assertIn("analysis_input_json", prompt)
        self.assertIn("禁止读取 SQLite", prompt)
        self.assertIn("你没有终端、文件或 memory 工具", prompt)
        self.assertIn("不写长期记忆", prompt)
        self.assertNotIn("quality-report", prompt)
        self.assertNotIn("--memory-candidates-only", prompt)
        self.assertNotIn("SQLite FTS", prompt)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", prompt)
        self.assertNotIn(str(self.root / "activity.sqlite"), prompt)
        row = self.conn.execute("SELECT * FROM quality_advice_runs").fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["issue_count"], 1)
        self.assertEqual(row["recommendation_count"], 1)
        stored = f"{row['summary_redacted']} {row['advice_json']}"
        self.assertNotIn("/Users/example", stored)
        self.assertNotIn(SYNTHETIC_SECRET_TOKEN, stored)
        self.assertIn("[REDACTED_PATH]", stored)
        self.assertIn("[REDACTED_SECRET]", stored)


    def test_quality_advice_report_does_not_hold_sqlite_writer_transaction(self) -> None:
        configure_quality_advice(self.conn, self.root, enabled=True)
        observed_transaction_states: list[bool] = []

        def report(conn, *_args, **_kwargs):
            observed_transaction_states.append(conn.in_transaction)
            return {
                "status": "ok",
                "summary": "bounded quality report",
                "learning": {},
                "document_signals": {},
            }

        with patch("activity_mvp.quality_advice.quality_report", side_effect=report), patch(
            "activity_mvp.quality_advice.generate_package",
            return_value={
                "package": {
                    "package_id": "quality-no-writer-package",
                    "source_status": "active",
                    "learning_digest": {},
                    "learnability_diagnostics": {},
                }
            },
        ), patch(
            "activity_mvp.quality_advice._get_package_locked",
            return_value={
                "package_id": "quality-no-writer-package",
                "source_status": "active",
                "learning_digest": {},
                "learnability_diagnostics": {},
            },
        ):
            result = run_quality_advice_once(
                self.conn,
                self.root,
                db_path=self.root / "activity.sqlite",
                start="2026-05-22T09:00:00+08:00",
                end="2026-05-22T09:12:00+08:00",
                force=True,
                trigger=False,
            )

        self.assertEqual(result["status"], "generated")
        self.assertEqual(observed_transaction_states, [False])


    def test_quality_advice_failure_does_not_raise_or_block_status(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_quality_advice(self.conn, self.root, enabled=True)
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="temporary hermes failure")
        with patch("activity_mvp.quality_advice.subprocess.run", return_value=failed):
            result = run_quality_advice_once(
                self.conn,
                self.root,
                db_path=self.root / "activity.sqlite",
                start="2026-05-22T09:00:00+08:00",
                end="2026-05-22T09:12:00+08:00",
                force=True,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_error"], "hermes_quality_advice_failed")
        status = quality_advice_status(self.conn, self.root)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["last_error"], "hermes_quality_advice_failed")
        self.assertNotIn("temporary hermes failure", json.dumps(status))


    def test_quality_advice_package_failure_persists_only_fixed_error_code(self) -> None:
        ingest_events(self.conn, EVENTS)
        configure_quality_advice(self.conn, self.root, enabled=True)
        secret = "/Volumes/customer/report.md " + ("c" * 64) + " raw customer sentence"

        with patch("activity_mvp.quality_advice.generate_package", side_effect=RuntimeError(secret)):
            result = run_quality_advice_once(
                self.conn,
                self.root,
                db_path=self.root / "activity.sqlite",
                start="2026-05-22T09:00:00+08:00",
                end="2026-05-22T09:12:00+08:00",
                force=True,
            )

        self.assertEqual(result["error"], "quality_advice_package_generation_failed")
        state = self.conn.execute("SELECT value FROM daemon_state WHERE key = 'quality_advice_last_error'").fetchone()["value"]
        self.assertEqual(state, "quality_advice_package_generation_failed")
        row = self.conn.execute("SELECT last_error FROM quality_advice_runs").fetchone()
        self.assertIsNone(row)
        self.assertNotIn(secret, json.dumps(result))


    def test_validate_quality_advice_checks_live_runtime_and_profile_without_tokens(self) -> None:
        configure_quality_advice(self.conn, self.root, enabled=True, profile="activity-quality-analysis")
        fake_home = self.root / "fake-home-quality"
        live_root = fake_home / "Library" / "Application Support" / "Personal AI Monitor" / "core"
        profile = fake_home / ".hermes" / "profiles" / "activity-quality-analysis"
        hermes = fake_home / "bin" / "hermes"
        live_root.mkdir(parents=True)
        profile.mkdir(parents=True)
        hermes.parent.mkdir(parents=True)
        hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hermes.chmod(0o700)
        with patch("activity_mvp.quality_advice.Path.home", return_value=fake_home), patch(
            "activity_mvp.ha_automation.Path.home",
            return_value=fake_home,
        ), patch(
            "activity_mvp.quality_advice.shutil.which",
            return_value=None,
        ):
            result = validate_quality_advice(self.conn, live_root)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertTrue(result["ready"])
        self.assertEqual(result["profile"], "activity-quality-analysis")
        self.assertNotIn("sk-", encoded)
        self.assertNotIn("token", encoded.lower())


    def test_get_package_handoff_summary_omits_full_activity_content(self) -> None:
        ingest_events(self.conn, EVENTS)
        self._manual_confirm(
            "Personal AI Monitor is implementing SQLite FTS and HA Source Package.",
            "2026-05-22T09:00:10+08:00",
        )
        result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )

        summary = get_package_handoff_summary(self.conn, result["package"]["package_id"], max_items=2)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertTrue(summary["handoff_summary"])
        self.assertEqual(summary["source_status"], "active")
        self.assertEqual(summary["activity_count"], 1)
        self.assertIn("representative_activities", summary)
        self.assertNotIn('"cleaned_content"', encoded)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", encoded)


    def test_get_package_memory_candidates_only_omits_activity_body_and_sensitive_residue(self) -> None:
        ingest_events(
            self.conn,
            [
                *EVENTS,
                {
                    "captured_at": "2026-05-22T09:01:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "bundle_id": "md.obsidian",
                    "window_title": "Personal AI Monitor memory decision",
                    "ocr_text": "Decision: Local Owner requires Personal AI Monitor to keep all long-term memory candidates review-only.",
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T09:01:45+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "bundle_id": "com.openai.codex",
                    "window_title": "Personal AI Monitor memory decision",
                    "ocr_text": "Decision confirmed by Local Owner: Personal AI Monitor keeps all long-term memory candidates review-only.",
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Decision: Local Owner requires Personal AI Monitor to keep all long-term memory candidates review-only.",
            "2026-05-22T09:01:10+08:00",
        )
        self._manual_confirm(
            "Decision confirmed by Local Owner: Personal AI Monitor keeps all long-term memory candidates review-only.",
            "2026-05-22T09:01:50+08:00",
        )
        result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )

        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=2)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertTrue(summary["memory_candidates_only"])
        self.assertEqual(summary["source_status"], "active")
        self.assertGreaterEqual(summary["memory_candidate_count"], 1)
        self.assertIn("memory_candidates", summary)
        self.assertIn("candidate_type", encoded)
        self.assertIn("why_keep", encoded)
        self.assertIn("project_tags", encoded)
        self.assertIn("write_eligibility", encoded)
        self.assertIn("anchor_type", encoded)
        self.assertIn("subject_scope", encoded)
        self.assertIn("subject_risk_flags", encoded)
        self.assertIn("write_risk_flags", encoded)
        self.assertEqual(summary["memory_candidates"][0]["write_eligibility"], "eligible")
        self.assertGreaterEqual(summary["memory_candidates"][0]["evidence_count"], 2)
        self.assertIn(summary["memory_candidates"][0]["subject_scope"], {"self_or_user_owned", "user_workflow_only"})
        self.assertIn("candidate_quality", summary)
        self.assertGreaterEqual(summary["eligible_candidate_count"], 1)
        self.assertIn("write_guard_summary", summary)
        self.assertIn("third_party_subject / ambiguous_subject 不进入 Hermes 写入输入", summary["write_guard_summary"])
        self.assertNotIn("representative_activities", summary)
        self.assertNotIn('"activities"', encoded)
        self.assertNotIn('"cleaned_content"', encoded)
        self.assertNotIn("REDACTED_TEST_CREDENTIAL_VALUE", encoded)
        self.assertNotIn("/Users/", encoded)


    def test_repeated_memory_candidates_semantically_merge_before_hermes_write(self) -> None:
        events = []
        base = datetime(2026, 6, 9, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        for index in range(45):
            at = (base + timedelta(minutes=index * 3)).isoformat(timespec="seconds")
            events.append(
                {
                    "captured_at": at,
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": f"Personal AI Monitor decision {index}",
                    "ocr_text": (
                        f"Local Owner Personal AI Monitor project fact {index}: decided to improve "
                        "DocumentSignal recall, Hermes quality advice, and memory candidate diagnostics."
                    ),
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                }
            )
        report = ingest_events(self.conn, events)
        self.assertEqual(report["clear"], 45)
        for index in range(45):
            self._manual_confirm(
                "Decision: Local Owner confirms Personal AI Monitor keeps context candidates merged before Hermes review.",
                (base + timedelta(seconds=index * 20 + 1)).isoformat(timespec="seconds"),
            )
        result = generate_package(
            self.conn,
            "2026-06-09T08:59:00+08:00",
            "2026-06-09T11:30:00+08:00",
            self.root / "packages",
        )
        candidates = result["package"]["learning_digest"]["memory_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["write_eligibility"], "eligible")
        self.assertEqual(candidates[0]["evidence_count"], 45)
        self.assertEqual(candidates[0]["merged_duplicate_count"], 44)
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=80)
        self.assertEqual(summary["memory_candidate_count"], 1)
        self.assertEqual(len(summary["memory_candidates"]), 1)
        self.assertGreaterEqual(summary["candidate_quality"]["merged_duplicate_count"], 44)
        for candidate in summary["memory_candidates"]:
            self.assertIn("candidate_type", candidate)
            self.assertIn("confidence", candidate)
            self.assertIn("why_keep", candidate)
            self.assertIn("project_tags", candidate)
            self.assertIn("anchor_type", candidate)
            self.assertIn("anchor_strength", candidate)
            self.assertIn("subject_scope", candidate)
            self.assertIn("subject_confidence", candidate)
            self.assertIn("subject_reasons", candidate)
            self.assertIn("subject_risk_flags", candidate)
            self.assertIn("quality_flags", candidate)
            self.assertIn("write_risk_flags", candidate)
            self.assertEqual(candidate["write_eligibility"], "eligible")
            self.assertIn("evidence_span_minutes", candidate)
            self.assertIn("merged_duplicate_count", candidate)


    def test_memory_candidates_merge_repeated_evidence_before_hermes_write(self) -> None:
        base = datetime(2026, 6, 9, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        events = []
        for offset in (0, 30, 60):
            events.append(
                {
                    "captured_at": (base + timedelta(seconds=offset)).isoformat(timespec="seconds"),
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Personal AI Monitor decision",
                    "ocr_text": (
                        "Decision: Local Owner requires Personal AI Monitor to keep memory candidates review-only. "
                        f"Observation {offset} confirms the same rule."
                    ),
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                }
            )
        ingest_events(self.conn, events)
        for index, offset in enumerate((0, 90, 180)):
            self._manual_confirm(
                "Decision: Local Owner confirms Personal AI Monitor merges repeated safe evidence before Hermes review.",
                (base + timedelta(seconds=offset + 1)).isoformat(timespec="seconds"),
            )
        result = generate_package(
            self.conn,
            "2026-06-09T08:59:00+08:00",
            "2026-06-09T09:05:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["memory_candidate_count"], 1)
        candidate = summary["memory_candidates"][0]
        self.assertEqual(candidate["write_eligibility"], "eligible")
        self.assertEqual(candidate["evidence_count"], 3)
        self.assertEqual(candidate["merged_duplicate_count"], 2)
        self.assertIn("merged_duplicate", candidate["quality_flags"])
        self.assertGreaterEqual(summary["candidate_quality"]["merged_duplicate_count"], 2)


    def test_single_unknown_media_and_system_candidates_are_not_eligible_for_hermes_write(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-09T09:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Unknown",
                    "window_title": "",
                    "ocr_text": "Generic page content without a stable project rule or reusable preference.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-09T09:03:00+08:00",
                    "display_id": "main",
                    "app_name": "IINA",
                    "window_title": "video",
                    "ocr_text": "Watching a media clip with transient visual content only.",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-09T09:06:00+08:00",
                    "display_id": "main",
                    "app_name": "System Settings",
                    "window_title": "settings",
                    "ocr_text": "System Settings permissions page with temporary toggles and no durable project rule.",
                    "context_confidence": 0.9,
                },
            ],
        )
        result = generate_package(
            self.conn,
            "2026-06-09T08:59:00+08:00",
            "2026-06-09T09:10:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertGreaterEqual(summary["ephemeral_candidate_count"], 1)
        self.assertEqual(summary["memory_candidate_skip_reason"], "third_party_or_ambiguous_subject")
        self.assertGreaterEqual(summary["subject_risk_candidate_count"], 1)
        self.assertEqual(summary["write_input_count"], 0)


    def test_iina_keyword_only_file_listing_is_demoted_before_hermes_write(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T12:39:00+08:00",
                    "display_id": "main",
                    "app_name": "IINA",
                    "window_title": "面试助手录制",
                    "ocr_text": (
                        "IINA 文件 编辑 回放 视频 音频 字幕 插件 窗口 帮助 "
                        "面试助手录制 F1.The.Movie.2025.4K.WEB-DL.mkv [path_redacted] "
                        "简历 面试 商务综合岗 替身 文件列表"
                    ),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-10T12:42:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Personal AI Monitor decision",
                    "ocr_text": (
                        "Decision: Local Owner requires Personal AI Monitor v0.1.6 to keep high recall but demote "
                        "IINA file listing noise before Hermes writes long-term memories."
                    ),
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-10T12:42:45+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Personal AI Monitor decision",
                    "ocr_text": (
                        "Decision confirmed by Local Owner: Personal AI Monitor v0.1.6 should keep high recall but demote "
                        "IINA file listing noise before Hermes writes long-term memories."
                    ),
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                },
            ],
        )
        self._manual_confirm(
            "Decision: Local Owner requires Personal AI Monitor v0.1.6 to keep high recall but demote IINA file listing noise before Hermes writes long-term memories.",
            "2026-06-10T12:42:10+08:00",
        )
        self._manual_confirm(
            "Decision confirmed by Local Owner: Personal AI Monitor v0.1.6 should keep high recall but demote IINA file listing noise before Hermes writes long-term memories.",
            "2026-06-10T12:42:50+08:00",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T12:38:00+08:00",
            "2026-06-10T12:45:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)
        self.assertNotIn("IINA", [candidate["app"] for candidate in summary["memory_candidates"]])
        self.assertGreaterEqual(summary["candidate_quality"]["media_or_file_listing_ephemeral_count"], 1)
        self.assertEqual(summary["candidate_quality"]["keyword_only_tag_count"], 0)
        self.assertGreaterEqual(summary["candidate_quality"]["single_evidence_demoted_count"], 1)
        self.assertEqual(summary["memory_candidates"][0]["app"], "Manual confirmation")
        self.assertEqual(summary["memory_candidates"][0]["anchor_type"], "trusted_app_context")
        self.assertGreaterEqual(summary["memory_candidates"][0]["evidence_count"], 2)


    def test_finder_folder_and_document_listing_do_not_create_confirmed_action(self) -> None:
        doc_path = self.root / "Alex_NVIDIA_Project_Manager.md"
        doc_path.write_text(
            "Alex Local Owner Resume Project Manager GTM channel operations self resume.",
            encoding="utf-8",
        )
        events = []
        for minute in (0, 1):
            event = {
                "captured_at": f"2026-06-10T12:{50 + minute}:00+08:00",
                "display_id": "main",
                "app_name": "Finder",
                "window_title": "Desktop",
                "ocr_text": "Desktop folders NVIDIA 项目经理 以色列市场 简历 文件夹 侧边栏",
                "document_path": str(doc_path),
                "context_confidence": 0.9,
            }
            document_signal = build_document_signal(self.root, event)
            self.assertIsNotNone(document_signal)
            event["document_signal"] = document_signal
            events.append(event)
        ingest_events(self.conn, events)
        result = generate_package(
            self.conn,
            "2026-06-10T12:49:00+08:00",
            "2026-06-10T12:55:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertGreaterEqual(summary["candidate_quality"]["noise_or_reference_count"], 1)
        self.assertGreaterEqual(summary["candidate_quality"]["finder_or_system_reference_count"], 1)
        region_counts = summary["candidate_quality"]["screen_region_distribution"]["region_kind_counts"]
        self.assertGreaterEqual(region_counts.get("desktop_icon", 0), 1)
        self.assertGreaterEqual(summary["candidate_quality"]["screen_region_noise_count"], 1)
        self.assertTrue(summary["rejected_observations"])


    def test_ticktick_old_task_is_reference_not_current_action(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T12:56:00+08:00",
                    "display_id": "main",
                    "app_name": "TickTick",
                    "window_title": "收集箱",
                    "ocr_text": "滴答清单 收集箱 今天 明天 已完成 调研以色列市场 NVIDIA 项目经理 添加任务",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-10T12:57:00+08:00",
                    "display_id": "main",
                    "app_name": "TickTick",
                    "window_title": "收集箱",
                    "ocr_text": "滴答清单 收集箱 今天 明天 已完成 调研以色列市场 NVIDIA 项目经理 添加任务",
                    "context_confidence": 0.9,
                },
            ],
        )
        result = generate_package(
            self.conn,
            "2026-06-10T12:55:00+08:00",
            "2026-06-10T13:00:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        # The current policy treats task-list completion labels as UI state rather than
        # a real-world occurrence that needs the user to adjudicate.
        confirmation = self.conn.execute(
            "SELECT event_type,state,region_kind,actionability,answer,reason_codes "
            "FROM activity_confirmation_items LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(confirmation)
        self.assertEqual(confirmation["event_type"], "completion")
        self.assertEqual((confirmation["state"], confirmation["answer"]), ("filtered", None))
        self.assertEqual(confirmation["region_kind"], "task_list")
        self.assertEqual(confirmation["actionability"], "context_only")
        self.assertIn("policy_v7_non_actionable_region", json.loads(confirmation["reason_codes"]))


    def test_repeated_chatgpt_research_discussion_can_be_confirmed_action(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T13:01:00+08:00",
                    "display_id": "main",
                    "app_name": "ChatGPT",
                    "window_title": "Israel market research",
                    "ocr_text": (
                        "Decision: Local Owner is actively researching the Israel robot vacuum market with ChatGPT, "
                        "comparing channels and competitors for a current market report."
                    ),
                    "screen_region_signal": actionable_layout("chat_thread"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-10T13:02:00+08:00",
                    "display_id": "main",
                    "app_name": "ChatGPT",
                    "window_title": "Israel market research",
                    "ocr_text": (
                        "Follow-up question: continue the Israel robot vacuum market research by checking "
                        "channel structure, competitors, and report outline in ChatGPT."
                    ),
                    "screen_region_signal": actionable_layout("chat_thread"),
                    "context_confidence": 0.9,
                },
            ],
        )
        self._seed_historical_confirmed_business_contexts(
            "2026-06-10T13:00:00+08:00",
            "2026-06-10T13:05:00+08:00",
            workstream="以色列市场研究",
            outcome="progressing",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T13:00:00+08:00",
            "2026-06-10T13:05:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertGreaterEqual(summary["eligible_candidate_count"], 1)
        candidate = summary["memory_candidates"][0]
        self.assertEqual(candidate["evidence_layer"], "confirmed_action")
        self.assertEqual(candidate["screen_region_signal"]["region_kind"], "chat_thread")
        self.assertEqual(candidate["screen_region_signal"]["actionability"], "actionable")
        self.assertIn("multi_evidence", candidate["confirmation_signals"])
        self.assertEqual(summary["confirmed_actions"], summary["memory_candidates"])


    def test_recently_modified_obsidian_document_can_be_confirmed_document_work(self) -> None:
        captured = datetime.now(timezone.utc).replace(microsecond=0)
        doc_path = self.root / "israel-market.md"
        doc_path.write_text(
            "Alex Local Owner 自用研究文档：决定继续调研以色列扫地机器人市场，整理渠道和竞品。",
            encoding="utf-8",
        )
        os.utime(doc_path, (captured.timestamp(), captured.timestamp()))
        events = []
        for offset in (0, 45):
            event_time = (captured + timedelta(seconds=offset)).isoformat(timespec="seconds")
            step_text = "第一轮整理渠道和竞品。" if offset == 0 else "第二轮继续补充渠道结构和竞品判断。"
            event = {
                "captured_at": event_time,
                "display_id": "main",
                "app_name": "Obsidian",
                "bundle_id": "md.obsidian",
                "window_title": "israel-market.md",
                "ocr_text": f"Alex Local Owner 自用研究文档：决定继续调研以色列扫地机器人市场，{step_text}",
                "document_path": str(doc_path),
                "screen_region_signal": actionable_layout("editor_body"),
                "context_confidence": 0.9,
            }
            document_signal = build_document_signal(self.root, event)
            self.assertIsNotNone(document_signal)
            self.assertEqual(document_signal["subsequent_mtime_event"], "modified_near_capture")
            event["document_signal"] = document_signal
            events.append(event)
        ingest_events(self.conn, events)
        self._seed_historical_confirmed_business_contexts(
            (captured - timedelta(minutes=1)).isoformat(timespec="seconds"),
            (captured + timedelta(minutes=2)).isoformat(timespec="seconds"),
            workstream="以色列市场研究",
            outcome="progressing",
        )
        result = generate_package(
            self.conn,
            (captured - timedelta(minutes=1)).isoformat(timespec="seconds"),
            (captured + timedelta(minutes=2)).isoformat(timespec="seconds"),
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertGreaterEqual(summary["eligible_candidate_count"], 1)
        candidate = summary["memory_candidates"][0]
        self.assertEqual(candidate["evidence_layer"], "confirmed_action")
        self.assertEqual(candidate["screen_region_signal"]["region_kind"], "editor_body")
        self.assertEqual(candidate["screen_region_signal"]["actionability"], "actionable")
        self.assertIn("document_modified_near_capture", candidate["confirmation_signals"])


    def test_single_trusted_app_weak_preference_is_review_only_before_hermes_write(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T13:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Personal AI Monitor notes",
                    "ocr_text": (
                        "Local Owner preference: compact dashboard panes are easier to scan during Personal AI Monitor work "
                        "when reviewing high level status cards."
                    ),
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                }
            ],
        )
        self._manual_confirm(
            "Local Owner preference: compact dashboard panes are easier to scan during Personal AI Monitor work when reviewing high level status cards.",
            "2026-06-10T13:00:10+08:00",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T12:59:00+08:00",
            "2026-06-10T13:05:00+08:00",
            self.root / "packages",
        )
        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertGreaterEqual(summary["review_only_candidate_count"], 1)
        review = summary["review_only_candidates"][0]
        self.assertEqual(review["candidate_type"], "preference")
        self.assertEqual(review["anchor_type"], "trusted_app_context")
        self.assertIn("single_evidence_demoted", review["quality_flags"])


    def test_keyword_project_tag_does_not_create_document_anchor(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T12:39:00+08:00",
                    "display_id": "main",
                    "app_name": "IINA",
                    "window_title": "面试素材",
                    "ocr_text": "面试 简历 公众号 草稿 文件列表 video.mp4 subtitles 字幕 回放",
                    "context_confidence": 0.9,
                }
            ],
        )
        result = generate_package(
            self.conn,
            "2026-06-10T12:38:00+08:00",
            "2026-06-10T12:45:00+08:00",
            self.root / "packages",
        )
        quality = result["package"]["learning_digest"]["memory_candidate_quality"]

        self.assertEqual(quality["keyword_only_tag_count"], 0)
        self.assertEqual(quality["trusted_anchor_eligible_count"], 0)
        self.assertGreaterEqual(quality["media_or_file_listing_ephemeral_count"], 1)
        self.assertEqual(get_package_memory_candidates(self.conn, result["package"]["package_id"])["memory_candidates"], [])


    def test_third_party_resume_is_not_eligible_for_hermes_memory_write(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T14:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Preview",
                    "window_title": "张三简历.pdf",
                    "ocr_text": "张三 简历 候选人 产品经理 工作经历 教育经历 项目经历 联系方式已隐藏",
                    "context_confidence": 0.9,
                }
            ],
        )
        self._seed_historical_confirmed_business_contexts(
            "2026-06-10T13:59:00+08:00",
            "2026-06-10T14:05:00+08:00",
            workstream="候选人材料审阅",
            outcome="completed",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T13:59:00+08:00",
            "2026-06-10T14:05:00+08:00",
            self.root / "packages",
        )

        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertEqual(summary["memory_candidate_skip_reason"], "third_party_or_ambiguous_subject")
        self.assertGreaterEqual(summary["candidate_quality"]["third_party_subject_count"], 1)
        self.assertGreaterEqual(summary["ephemeral_candidate_count"], 1)


    def test_self_owned_resume_signal_keeps_subject_scope_without_raw_contact_details(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T14:10:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Alex 自我介绍 - Obsidian",
                    "ocr_text": (
                        "Alex Local Owner 自我介绍 简历：决定默认用口语版自我介绍准备产品经理面试，"
                        "只保留角色定位和准备规则，不包含联系方式。"
                    ),
                    "screen_region_signal": actionable_layout("editor_body"),
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-06-10T14:10:45+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "Alex 自我介绍 - Personal AI Monitor",
                    "ocr_text": (
                        "Alex Local Owner 自我介绍 简历：再次确认默认用口语版自我介绍准备产品经理面试，"
                        "只保留角色定位和准备规则，不包含联系方式。"
                    ),
                    "screen_region_signal": actionable_layout("main_content"),
                    "context_confidence": 0.9,
                }
            ],
        )
        self._seed_historical_confirmed_business_contexts(
            "2026-06-10T14:09:00+08:00",
            "2026-06-10T14:15:00+08:00",
            workstream="面试准备",
            outcome="progressing",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T14:09:00+08:00",
            "2026-06-10T14:15:00+08:00",
            self.root / "packages",
        )

        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)
        encoded = json.dumps(summary, ensure_ascii=False)

        self.assertGreaterEqual(summary["eligible_candidate_count"], 1)
        candidate = summary["memory_candidates"][0]
        self.assertEqual(candidate["subject_scope"], "self_or_user_owned")
        self.assertEqual(candidate["write_eligibility"], "eligible")
        self.assertNotIn("user@example.com", encoded)
        self.assertNotIn("/Users/", encoded)


    def test_single_resume_or_interview_material_without_self_subject_is_not_write_input(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T14:16:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "岗位面试材料 - Obsidian",
                    "ocr_text": (
                        "简历 面试 岗位材料：决定默认保留产品经理面试准备规则，"
                        "包括角色定位、项目经历和表达结构，不包含联系方式。"
                    ),
                    "context_confidence": 0.9,
                }
            ],
        )
        self._seed_historical_confirmed_business_contexts(
            "2026-06-10T14:15:00+08:00",
            "2026-06-10T14:20:00+08:00",
            workstream="面试材料整理",
            outcome="progressing",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T14:15:00+08:00",
            "2026-06-10T14:20:00+08:00",
            self.root / "packages",
        )

        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertGreaterEqual(summary["candidate_quality"]["ambiguous_subject_count"], 1)
        self.assertGreaterEqual(summary["subject_risk_candidate_count"], 1)
        self.assertEqual(summary["review_only_candidates"], [])
        self.assertGreaterEqual(summary["ephemeral_candidate_count"], 1)
        self.assertEqual(summary["memory_candidate_skip_reason"], "third_party_or_ambiguous_subject")
        self.assertEqual(summary["write_input_count"], 0)


    def test_external_browser_resume_defaults_to_ambiguous_subject_not_write_input(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-10T14:20:00+08:00",
                    "display_id": "main",
                    "app_name": "Google Chrome",
                    "window_title": "Resume portfolio - Chrome",
                    "ocr_text": "Resume portfolio education work history leadership operations interview notes",
                    "context_confidence": 0.9,
                }
            ],
        )
        self._seed_historical_confirmed_business_contexts(
            "2026-06-10T14:19:00+08:00",
            "2026-06-10T14:25:00+08:00",
            workstream="简历资料浏览",
            outcome="progressing",
        )
        result = generate_package(
            self.conn,
            "2026-06-10T14:19:00+08:00",
            "2026-06-10T14:25:00+08:00",
            self.root / "packages",
        )

        summary = get_package_memory_candidates(self.conn, result["package"]["package_id"], max_items=40)

        self.assertEqual(summary["eligible_candidate_count"], 0)
        self.assertEqual(summary["memory_candidates"], [])
        self.assertGreaterEqual(summary["candidate_quality"]["ambiguous_subject_count"], 1)
        self.assertEqual(summary["review_only_candidates"], [])
        self.assertGreaterEqual(summary["ephemeral_candidate_count"], 1)
        self.assertEqual(summary["memory_candidate_skip_reason"], "third_party_or_ambiguous_subject")
        self.assertEqual(summary["write_input_count"], 0)


    def test_exact_cleaned_duplicate_becomes_content_free_duplicate_event(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:00:00+08:00",
                    "display_id": "main",
                    "frame_hash": "hash-a",
                    "app_name": "Obsidian",
                    "window_title": "重复页",
                    "ocr_text": "完全重复内容\n第二行",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T10:00:15+08:00",
                    "display_id": "main",
                    "frame_hash": "hash-b",
                    "app_name": "Obsidian",
                    "window_title": "重复页",
                    "ocr_text": "完全重复内容\n第二行",
                    "context_confidence": 0.9,
                },
            ],
        )

        self.assertEqual(report["clear"], 1)
        self.assertEqual(report["duplicate_exact"], 1)
        self.assertEqual(len(timeline(self.conn)), 1)
        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T09:59:00+08:00",
            "2026-05-22T10:01:00+08:00",
        )
        self.assertEqual(len(export["duplicate_events"]), 1)
        markdown = render_cleaned_timeline_markdown(export)
        self.assertIn("重复清洗", markdown)
        self.assertIn("与上一条同显示器截屏内容完全重复", markdown)

        package = generate_package(
            self.conn,
            "2026-05-22T09:59:00+08:00",
            "2026-05-22T10:01:00+08:00",
            self.root / "packages",
        )["package"]
        self.assertEqual([item["kind"] for item in package["safe_timeline_events"]], ["unverified_clue", "duplicate"])
        self.assertEqual(package["safe_timeline_events"][1]["duplicate_count"], 1)


    def test_partial_duplicate_keeps_only_changed_cleaned_lines(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:10:00+08:00",
                    "display_id": "main",
                    "app_name": "Typora",
                    "window_title": "部分重复",
                    "ocr_text": "A\nB\nC",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T10:10:15+08:00",
                    "display_id": "main",
                    "app_name": "Typora",
                    "window_title": "部分重复",
                    "ocr_text": "A\nB\nD",
                    "context_confidence": 0.9,
                },
            ],
        )

        self.assertEqual(report["clear"], 2)
        self.assertEqual(report["partial_duplicate"], 1)
        activities = timeline(self.conn)
        self.assertEqual(activities[1]["dedupe_status"], "partial_duplicate")
        self.assertEqual(activities[1]["cleaned_text"], "D")
        self.assertIn("已省略", activities[1]["dedupe_note"])
        self.assertEqual(
            search(
                self.conn,
                "B",
                start="2026-05-22T10:10:10+08:00",
                end="2026-05-22T10:10:30+08:00",
            ),
            [],
        )
        self.assertEqual(
            len(
                search(
                    self.conn,
                    "D",
                    start="2026-05-22T10:10:10+08:00",
                    end="2026-05-22T10:10:30+08:00",
                )
            ),
            1,
        )
        markdown = render_cleaned_timeline_markdown(
            cleaned_timeline_export(
                self.conn,
                "2026-05-22T10:10:10+08:00",
                "2026-05-22T10:10:30+08:00",
            )
        )
        self.assertIn("重复清洗：已省略 2 行", markdown)
        second_section = markdown.split("### 2. 活动", 1)[1]
        self.assertIn("\nD\n", second_section)
        self.assertNotIn("\nA\n", second_section)


    def test_blocked_frames_do_not_become_duplicates(self) -> None:
        report = ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:20:00+08:00",
                    "display_id": "main",
                    "app_name": "1Password",
                    "window_title": "Secure Note",
                    "ocr_text": "same sensitive text",
                    "secure_input": True,
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-22T10:20:15+08:00",
                    "display_id": "main",
                    "app_name": "1Password",
                    "window_title": "Secure Note",
                    "ocr_text": "same sensitive text",
                    "secure_input": True,
                    "context_confidence": 0.9,
                },
            ],
        )

        self.assertEqual(report["blocked"], 2)
        self.assertEqual(report["duplicate_exact"], 0)
        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T10:19:00+08:00",
            "2026-05-22T10:21:00+08:00",
        )
        self.assertEqual(export["duplicate_events"], [])
        self.assertEqual(len(export["privacy_events"]), 2)


    def test_time_range_filters_normalize_mixed_offsets(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-23T22:59:00+08:00",
                    "display_id": "main",
                    "app_name": "Notes",
                    "window_title": "Previous local day",
                    "ocr_text": "Previous local day marker",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": "2026-05-24T00:10:00+08:00",
                    "display_id": "main",
                    "app_name": "Notes",
                    "window_title": "Current local day",
                    "ocr_text": "Current local day marker",
                    "context_confidence": 0.9,
                },
            ],
        )
        start = "2026-05-23T16:00:00Z"
        end = "2026-05-24T16:00:00Z"

        review = daily_review(self.conn, start, end)
        self.assertEqual(review["activity_count"], 1)
        self.assertEqual(len(search(self.conn, "Previous local day", start=start, end=end)), 0)
        self.assertEqual(len(search(self.conn, "Current local day", start=start, end=end)), 1)

        deleted = delete_range(self.conn, start, end)
        self.assertEqual(deleted["activities_deleted"], 1)
        self.assertEqual(len(search(self.conn, "Previous local day")), 1)
        self.assertEqual(len(search(self.conn, "Current local day")), 0)


    def test_package_redacts_local_paths_from_clear_activity(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T10:20:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "项目整理",
                    "ocr_text": "正在整理 /Users/example/Documents/example-note.md 的摘要",
                    "context_confidence": 0.9,
                }
            ],
        )

        package_result = generate_package(
            self.conn,
            "2026-05-22T10:20:00+08:00",
            "2026-05-22T10:25:00+08:00",
            self.root / "packages",
        )
        package_text = Path(package_result["json_path"]).read_text(encoding="utf-8")

        self.assertIn("unverified_clue", package_text)
        self.assertNotIn("[path_redacted]", package_text)
        self.assertNotIn("/Users/", package_text)


    def test_daemon_pause_skips_capture(self) -> None:
        db_path = self.root / "activity.sqlite"
        pause(self.conn, minutes=1)
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon.capture_sck_keyframe_batch"
        ) as capture:
            result = run_once(db_path)
        self.assertEqual(result["status"], "paused")
        capture.assert_not_called()
        resume(self.conn)


    def test_screen_capture_disable_keeps_file_tracking_unpaused(self) -> None:
        db_path = self.root / "activity.sqlite"
        disabled = configure_screen_capture(self.conn, enabled=False)
        self.assertFalse(disabled["screen_capture_enabled"])
        self.assertTrue(disabled["document_change_tracking_enabled"])
        self.assertEqual(disabled["pause_mode"], "")

        with patch("activity_mvp.daemon.session_state") as session_probe, patch(
            "activity_mvp.daemon.capture_sck_keyframe_batch"
        ) as capture:
            result = run_once(db_path)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["captured"], 0)
        session_probe.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM daemon_state WHERE key='pause_mode'"
            ).fetchone()["value"],
            "",
        )

        resumed = resume(self.conn)
        self.assertFalse(resumed["paused"])
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM settings WHERE key='screen_capture_enabled'"
            ).fetchone()["value"],
            "false",
        )

        configure_screen_capture(self.conn, enabled=False)
        enabled = configure_screen_capture(self.conn, enabled=True)
        self.assertTrue(enabled["screen_capture_enabled"])


    def test_pause_modes_cover_today_until_lock_and_resume(self) -> None:
        self.seed_runtime_progress()
        today = pause(self.conn, mode="today")
        self.assertEqual(today["pause_mode"], "today")
        self.assertIn("今天不记录", today["pause_label"])
        resumed = resume(self.conn)
        self.assertFalse(resumed["paused"])

        manual = pause(self.conn, mode="manual")
        self.assertEqual(manual["pause_mode"], "manual")
        self.assertEqual(manual["pause_label"], "暂停（直到手动开启）")
        self.assertEqual(manual["pause_until"], "")
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ):
            status = get_status(self.conn, self.root)
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["pause_mode"], "manual")
        self.assertEqual(status["pause_label"], "暂停（直到手动开启）")
        resumed = resume(self.conn)
        self.assertFalse(resumed["paused"])

        until_lock = pause(self.conn, mode="until_lock")
        self.assertEqual(until_lock["pause_mode"], "until_lock")
        self.assertEqual(until_lock["pause_label"], "暂停到下次锁屏")
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ):
            status = get_status(self.conn, self.root)
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["pause_mode"], "until_lock")

        with patch("activity_mvp.daemon.session_state", return_value={"locked": True}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ):
            status = get_status(self.conn, self.root)
        self.assertEqual(status["status"], "locked")
        self.assertEqual(status["pause_mode"], "")


    def test_pause_clears_last_error_and_status_prefers_pause(self) -> None:
        self.seed_runtime_progress()
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_error", "sck_keyframe timed out", now),
        )
        self.conn.commit()

        pause(self.conn, mode="today")
        row = self.conn.execute("SELECT value FROM daemon_state WHERE key = 'last_error'").fetchone()
        self.assertEqual(row["value"], "")

        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_error", "legacy sck_keyframe timeout", now),
        )
        self.conn.commit()
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ):
            status = get_status(self.conn, self.root)

        self.assertEqual(status["status"], "paused")
        self.assertIn("暂停期间", status["status_explanation"])
        self.assertNotIn("legacy sck_keyframe timeout", status["status_explanation"])
        codes = {item["code"] for item in status["attention_items"]}
        self.assertNotIn("last_error", codes)


    def test_daemon_discards_if_locked_after_capture(self) -> None:
        db_path = self.root / "activity.sqlite"
        fake_batch = {
            "events": [
                {
                    "captured_at": "2026-05-22T10:30:00+08:00",
                    "display_id": "sck-display-1",
                    "frame_hash": "late-lock",
                    "app_name": "Obsidian",
                    "bundle_id": "md.obsidian",
                    "window_title": "锁屏中止",
                    "url": "",
                    "domain": "",
                    "ocr_text": "这轮不应该写入",
                    "context_confidence": 0.8,
                }
            ],
            "skipped": [],
            "errors": [],
        }
        with patch(
            "activity_mvp.daemon.session_state",
            side_effect=[{"locked": False}, {"locked": True}],
        ), patch("activity_mvp.daemon.capture_sck_keyframe_batch", return_value=fake_batch):
            result = run_once(db_path)
        self.assertEqual(result["status"], "locked_after_capture")
        self.assertEqual(result["discarded"], 1)
        self.assertEqual(search(self.conn, "这轮不应该写入"), [])


    def test_daemon_dedupes_same_display_hash(self) -> None:
        db_path = self.root / "activity.sqlite"

        def fake_capture(root, should_process=None, retain_for_ingest=False):
            metadata = {
                "captured_at": "2026-05-22T11:00:00+08:00",
                "display_id": "sck-display-1",
                "frame_hash": "abc123",
                "capture_width": 1280,
                "capture_height": 720,
            }
            if should_process is not None and not should_process(metadata):
                return {"events": [], "skipped": [{**metadata, "reason": "duplicate_frame"}], "errors": []}
            return {
                "events": [
                    {
                        **metadata,
                        "app_name": "Obsidian",
                        "bundle_id": "md.obsidian",
                        "window_title": "去重测试",
                        "url": "",
                        "domain": "",
                        "ocr_text": "第一次画面",
                        "context_confidence": 0.8,
                        "url_source": "test",
                        "context_source": "test",
                    }
                ],
                "skipped": [],
                "errors": [],
            }

        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon.capture_sck_keyframe_batch", side_effect=fake_capture
        ):
            first = run_once(db_path)
            second = run_once(db_path)
        self.assertEqual(first["captured"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len(timeline(self.conn)), 1)
        export = cleaned_timeline_export(
            self.conn,
            "2026-05-22T10:59:00+08:00",
            "2026-05-22T11:01:00+08:00",
        )
        self.assertEqual(len(export["duplicate_events"]), 1)
        self.assertIn("跳过 OCR", export["duplicate_events"][0]["note"])


    def test_multidisplay_ocr_processes_every_captured_display(self) -> None:
        main_image = self.root / "main.png"
        secondary_image = self.root / "secondary.png"
        main_image.write_bytes(b"main")
        secondary_image.write_bytes(b"secondary")
        main_image.chmod(0o644)
        secondary_image.chmod(0o644)

        class Completed:
            returncode = 0
            stderr = ""
            stdout = ""

        completed = Completed()
        completed.stdout = json_dumps(
            [
                {
                    "display_id": "1",
                    "status": "captured",
                    "image_path": str(main_image),
                    "frame_hash": "main-hash",
                    "is_main": True,
                    "width": 1280,
                    "height": 720,
                    "display_frame": {"x": 0, "y": 0, "width": 1280, "height": 720},
                    "visible_windows": [],
                    "error": None,
                },
                {
                    "display_id": "2",
                    "status": "captured",
                    "image_path": str(secondary_image),
                    "frame_hash": "secondary-hash",
                    "is_main": False,
                    "width": 1280,
                    "height": 720,
                    "display_frame": {"x": 1280, "y": 0, "width": 1280, "height": 720},
                    "visible_windows": [
                        {
                            "window_id": "movie-window",
                            "app_name": "IINA",
                            "bundle_id": "com.colliderli.iina",
                            "title": "Raw Movie Title Should Not Persist In Visible Windows",
                            "frame": {"x": 1280, "y": 0, "width": 1280, "height": 720},
                            "overlap_ratio": 0.92,
                        }
                    ],
                    "error": None,
                },
            ]
        )
        def fake_ocr_payload(path):
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            if path == main_image:
                self.assertEqual(oct(secondary_image.stat().st_mode & 0o777), "0o600")
                return {
                    "lines": [
                        {
                            "text": "主屏内容",
                            "confidence": 0.95,
                            "bounding_box": {"x": 0.3, "y": 0.4, "width": 0.4, "height": 0.1},
                        }
                    ],
                    "coordinate_space": "vision_normalized_bottom_left",
                }
            return {
                "lines": [
                    {
                        "text": "副屏内容",
                        "confidence": 0.95,
                        "bounding_box": {"x": 0.3, "y": 0.4, "width": 0.4, "height": 0.1},
                    }
                ],
                "coordinate_space": "vision_normalized_bottom_left",
            }

        with patch("activity_mvp.macos_probe.helper_command", return_value=["helper"]), patch(
            "activity_mvp.macos_probe.subprocess.run", return_value=completed
        ), patch("activity_mvp.macos_probe._frontmost_context", return_value={"app_name": "Obsidian", "bundle_id": "", "window_title": ""}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "https://front.example/[redacted]", "domain": "front.example", "source": "test"},
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch(
            "activity_mvp.macos_probe.submit_ocr",
            side_effect=lambda _root, _command, path: ImmediateOcrTask(
                OcrOutcome(payload=fake_ocr_payload(Path(path)))
            ),
        ) as ocr:
            batch = capture_sck_keyframe_batch(self.root, should_process=lambda metadata: True)
        self.assertEqual(len(batch["events"]), 2)
        self.assertEqual(batch["skipped"], [])
        self.assertEqual([event["ocr_text"] for event in batch["events"]], ["主屏内容", "副屏内容"])
        self.assertEqual([event["ocr_blocks"][0]["display_id"] for event in batch["events"]], ["sck-display-1", "sck-display-2"])
        self.assertEqual(batch["events"][0]["domain"], "front.example")
        self.assertEqual(batch["events"][1]["app_name"], "IINA")
        self.assertEqual(batch["events"][1]["domain"], "")
        self.assertEqual(batch["events"][1]["url_source"], "display_ocr_only")
        self.assertEqual(batch["events"][1]["display_attribution"]["source"], "screencapturekit_keyframe+display_window_match")
        self.assertEqual(batch["events"][1]["visible_windows"][0]["app_name"], "IINA")
        self.assertNotIn("title", batch["events"][1]["visible_windows"][0])
        self.assertEqual(ocr.call_count, 2)
        self.assertFalse(main_image.exists())
        self.assertFalse(secondary_image.exists())


    def test_display_frame_contexts_store_safe_window_attribution_only(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-04T09:00:00+08:00",
                    "display_id": "external",
                    "app_name": "IINA",
                    "bundle_id": "com.colliderli.iina",
                    "window_title": "Safe Media Window",
                    "ocr_text": "media controls",
                    "visible_windows": [
                        {
                            "window_id": "w1",
                            "app_name": "IINA",
                            "bundle_id": "com.colliderli.iina",
                        "title": "Raw Visible Window Title Should Not Persist",
                            "frame": {"x": 10, "y": 20, "width": 640, "height": 360},
                            "overlap_ratio": 0.8,
                        }
                    ],
                    "display_attribution": {
                        "app_name": "IINA",
                        "bundle_id": "com.colliderli.iina",
                        "window_id": "w1",
                        "window_title": "Raw Display Attribution Title Should Not Persist",
                        "confidence": 0.68,
                        "source": "screencapturekit_keyframe+display_window_match",
                        "reason_codes": ["display_window_match"],
                    },
                }
            ],
        )

        context = self.conn.execute(
            "SELECT visible_windows, app_contexts FROM display_frame_contexts ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()
        visible = json.loads(context["visible_windows"])
        app_contexts = json.loads(context["app_contexts"])
        encoded = json.dumps({"visible": visible, "app_contexts": app_contexts}, ensure_ascii=False)

        self.assertEqual(visible[0]["app_name"], "IINA")
        self.assertTrue(visible[0]["has_title"])
        self.assertNotIn("title", visible[0])
        self.assertEqual(app_contexts["display_attribution"]["source"], "screencapturekit_keyframe+display_window_match")
        self.assertNotIn("Raw Visible Window Title", encoded)
        self.assertNotIn("Raw Display Attribution Title", encoded)


    def test_secondary_browser_domain_uses_unique_safe_signal(self) -> None:
        image = self.root / "secondary-browser.png"
        image.write_bytes(b"browser image")
        completed = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                [
                    {
                        "status": "captured",
                        "image_path": str(image),
                        "display_id": 2,
                        "frame_hash": "secondary-browser-hash",
                        "is_main": False,
                        "width": 1280,
                        "height": 720,
                        "display_frame": {"x": 1280, "y": 0, "width": 1280, "height": 720},
                        "visible_windows": [
                            {
                                "window_id": "dia-window",
                                "app_name": "Dia",
                                "bundle_id": "company.thebrowser.dia",
                                "title": "Raw Browser Title Should Not Persist",
                                "frame": {"x": 1280, "y": 0, "width": 1280, "height": 720},
                                "overlap_ratio": 0.95,
                            }
                        ],
                    }
                ]
            ),
        )
        signal = sanitize_browser_signal(
            {
                "browser": "Dia",
                "bundle_id": "company.thebrowser.dia",
                "url": "https://docs.example.com/private?q=token",
                "domain": "docs.example.com",
                "window_id": "browser-window",
                "tab_id": "tab-1",
                "source": "chromium_extension_native_host",
                "confidence": 0.94,
            }
        )

        with patch("activity_mvp.macos_probe.helper_command", return_value=["helper"]), patch(
            "activity_mvp.macos_probe.subprocess.run", return_value=completed
        ), patch("activity_mvp.macos_probe._frontmost_context", return_value={"app_name": "Finder", "bundle_id": "", "window_title": ""}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "", "domain": "", "source": "not_browser"},
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch(
            "activity_mvp.macos_probe.submit_ocr",
            side_effect=lambda _root, _command, _path: ImmediateOcrTask(
                OcrOutcome(payload={"lines": [], "coordinate_space": "vision"})
            ),
        ), patch(
            "activity_mvp.macos_probe.list_recent_browser_signals",
            return_value=[signal],
        ):
            batch = capture_sck_keyframe_batch(self.root, should_process=lambda metadata: True)

        event = batch["events"][0]
        self.assertEqual(event["app_name"], "Dia")
        self.assertEqual(event["domain"], "docs.example.com")
        self.assertEqual(event["url_source"], "chromium_extension_native_host")
        self.assertIn("browser_signal_display_match", event["display_attribution"]["reason_codes"])
        encoded = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("private?q=token", encoded)
        self.assertFalse(image.exists())


    def test_secondary_browser_domain_ambiguous_signal_stays_unattributed(self) -> None:
        image = self.root / "secondary-browser-ambiguous.png"
        image.write_bytes(b"browser image")
        completed = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                [
                    {
                        "status": "captured",
                        "image_path": str(image),
                        "display_id": 2,
                        "frame_hash": "secondary-browser-ambiguous",
                        "is_main": False,
                        "width": 1280,
                        "height": 720,
                        "display_frame": {"x": 1280, "y": 0, "width": 1280, "height": 720},
                        "visible_windows": [
                            {
                                "window_id": "dia-window",
                                "app_name": "Dia",
                                "bundle_id": "company.thebrowser.dia",
                                "title": "Raw Browser Title Should Not Persist",
                                "frame": {"x": 1280, "y": 0, "width": 1280, "height": 720},
                                "overlap_ratio": 0.95,
                            }
                        ],
                    }
                ]
            ),
        )
        signals = [
            sanitize_browser_signal(
                {
                    "browser": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "url": "https://one.example.com/page",
                    "domain": "one.example.com",
                    "window_id": "browser-window-1",
                    "tab_id": "tab-1",
                }
            ),
            sanitize_browser_signal(
                {
                    "browser": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "url": "https://two.example.com/page",
                    "domain": "two.example.com",
                    "window_id": "browser-window-2",
                    "tab_id": "tab-2",
                }
            ),
        ]

        with patch("activity_mvp.macos_probe.helper_command", return_value=["helper"]), patch(
            "activity_mvp.macos_probe.subprocess.run", return_value=completed
        ), patch("activity_mvp.macos_probe._frontmost_context", return_value={"app_name": "Finder", "bundle_id": "", "window_title": ""}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "", "domain": "", "source": "not_browser"},
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch(
            "activity_mvp.macos_probe.submit_ocr",
            side_effect=lambda _root, _command, _path: ImmediateOcrTask(
                OcrOutcome(payload={"lines": [], "coordinate_space": "vision"})
            ),
        ), patch(
            "activity_mvp.macos_probe.list_recent_browser_signals",
            return_value=signals,
        ):
            batch = capture_sck_keyframe_batch(self.root, should_process=lambda metadata: True)

        event = batch["events"][0]
        self.assertEqual(event["domain"], "")
        self.assertEqual(event["url_source"], "display_ocr_only")
        self.assertIn("browser_signal_ambiguous", event["display_attribution"]["reason_codes"])
        self.assertFalse(image.exists())


    def test_document_startup_reconcile_defaults_on_but_explicit_false_disables(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_document_change_startup_reconcile_enabled({}))
        with patch.dict(
            os.environ,
            {DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY: "0"},
            clear=True,
        ):
            self.assertFalse(
                _document_change_startup_reconcile_enabled(
                    {DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY: "1"}
                )
            )
        with patch.dict(
            os.environ,
            {DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY: "1"},
            clear=True,
        ):
            self.assertTrue(_document_change_startup_reconcile_enabled({}))


    def test_document_worker_startup_eligibility_gates_on_session_and_low_power(self) -> None:
        """Session and Low Power Mode gate startup; battery use no longer does.

        The former AC + 120s-idle gate deferred the one-time baseline scan to
        an unattended window, but in practice it meant tracking never restarted
        after a daemon restart and the baseline stayed permanently partial.
        Baseline I/O is paced by BASELINE_SCAN_THROTTLE_* instead, so the only
        power signal that stops work now is the owner asking for Low Power Mode.
        """
        eligible_session = {
            "state": "unlocked",
            "locked": False,
            "on_console": True,
            "capture_allowed": True,
        }
        for label, power, idle in (
            (
                "on ac",
                {"external_power": True, "source": "ac_power"},
                {"available": True, "idle_seconds": 0},
            ),
            (
                "on battery",
                {"external_power": False, "source": "battery_power"},
                {"available": True, "idle_seconds": 0},
            ),
            (
                "battery with the owner actively typing",
                {"external_power": False, "source": "battery_power"},
                {"available": True, "idle_seconds": 0},
            ),
            (
                "idle probe unavailable",
                {"external_power": False, "source": "battery_power"},
                {"available": False, "idle_seconds": 0},
            ),
            (
                "legacy power payload without the low power key",
                {"external_power": False},
                {"available": True, "idle_seconds": 0},
            ),
        ):
            with self.subTest(allowed=label):
                result = _document_change_worker_startup_eligibility(
                    self.root,
                    session=eligible_session,
                    power=power,
                    idle=idle,
                )
                self.assertTrue(result["allowed"], label)
                self.assertEqual(result["reason"], "")

        locked_console = _document_change_worker_startup_eligibility(
            self.root,
            session={
                **eligible_session,
                "state": "locked",
                "locked": True,
                "capture_allowed": False,
            },
            power={"external_power": True, "low_power_mode": False},
            idle={"available": True, "idle_seconds": 0},
        )
        self.assertTrue(locked_console["allowed"])
        self.assertEqual(locked_console["reason"], "")

        denied_cases = (
            (
                {**eligible_session, "on_console": False},
                {"external_power": True},
                {"available": True, "idle_seconds": 999},
                "session_not_eligible",
            ),
            (
                eligible_session,
                {"external_power": True, "source": "ac_power", "low_power_mode": True},
                {"available": True, "idle_seconds": 999},
                "low_power_mode_active",
            ),
            (
                eligible_session,
                {"external_power": False, "source": "battery_power", "low_power_mode": True},
                {"available": True, "idle_seconds": 999},
                "low_power_mode_active",
            ),
        )
        for session, power, idle, reason in denied_cases:
            with self.subTest(reason=reason, session=session, power=power, idle=idle):
                result = _document_change_worker_startup_eligibility(
                    self.root,
                    session=session,
                    power=power,
                    idle=idle,
                )
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], reason)


    def test_eligible_document_worker_wrapper_never_starts_early(self) -> None:
        with patch(
            "activity_mvp.daemon._document_change_worker_startup_eligibility",
            return_value={
                "allowed": False,
                "reason": "idle_required",
                "raw_access": False,
            },
        ), patch(
            "activity_mvp.daemon._maybe_start_document_change_worker",
            return_value=True,
        ) as worker:
            denied = _maybe_start_document_change_worker_when_eligible(
                self.root / "activity.sqlite",
                self.root,
            )
        self.assertFalse(denied["started"])
        worker.assert_not_called()

        with patch(
            "activity_mvp.daemon._document_change_worker_startup_eligibility",
            return_value={
                "allowed": True,
                "reason": "",
                "raw_access": False,
            },
        ), patch(
            "activity_mvp.daemon._maybe_start_document_change_worker",
            return_value=True,
        ) as worker:
            started = _maybe_start_document_change_worker_when_eligible(
                self.root / "activity.sqlite",
                self.root,
            )
        self.assertTrue(started["started"])
        worker.assert_called_once_with(self.root / "activity.sqlite")


    def test_document_worker_short_exit_starts_retry_cooldown(self) -> None:
        from activity_mvp import daemon as module

        with patch.object(
            module,
            "_DOCUMENT_CHANGE_WORKER_THREAD",
            None,
        ), patch.object(
            module,
            "_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC",
            0.0,
        ), patch(
            "activity_mvp.document_changes.start_document_change_worker",
            return_value={"started": False, "reason": "baseline_not_clean"},
        ), patch(
            "activity_mvp.daemon.helper_command",
            return_value=["/safe/fsevents-helper"],
        ), patch(
            "activity_mvp.daemon.time.monotonic",
            return_value=100.0,
        ):
            self.assertTrue(
                module._maybe_start_document_change_worker(
                    self.root / "activity.sqlite"
                )
            )
            worker_thread = module._DOCUMENT_CHANGE_WORKER_THREAD
            self.assertIsNotNone(worker_thread)
            worker_thread.join(timeout=3)
            self.assertFalse(worker_thread.is_alive())
            self.assertEqual(
                module._DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                100.0 + module.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
            )


    def test_document_worker_waits_for_full_pipeline_before_retry(self) -> None:
        from activity_mvp import daemon as module

        names = ("reader", "consumer", "validator", "processor", "monitor")
        releases = {name: threading.Event() for name in names}
        completed = {name: threading.Event() for name in names}

        def wait_for_release(name: str) -> None:
            releases[name].wait()
            completed[name].set()

        threads = {
            name: threading.Thread(
                target=wait_for_release,
                args=(name,),
                name=f"test-document-{name}",
                daemon=True,
            )
            for name in names
        }
        for thread in threads.values():
            thread.start()

        result = {"started": True, **threads}
        worker_started = threading.Event()

        def start_worker_result(*args, **kwargs):
            worker_started.set()
            return result

        outer_thread = None
        try:
            with patch.object(
                module,
                "_DOCUMENT_CHANGE_WORKER_THREAD",
                None,
            ), patch.object(
                module,
                "_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC",
                0.0,
            ), patch(
                "activity_mvp.document_changes.start_document_change_worker",
                side_effect=start_worker_result,
            ) as start_worker, patch(
                "activity_mvp.daemon.helper_command",
                return_value=["/safe/fsevents-helper"],
            ), patch(
                "activity_mvp.daemon.time.monotonic",
                return_value=100.0,
            ):
                self.assertTrue(
                    module._maybe_start_document_change_worker(
                        self.root / "activity.sqlite"
                    )
                )
                outer_thread = module._DOCUMENT_CHANGE_WORKER_THREAD
                self.assertIsNotNone(outer_thread)
                self.assertTrue(worker_started.wait(timeout=2))
                self.assertTrue(outer_thread.is_alive())
                self.assertFalse(
                    module._maybe_start_document_change_worker(
                        self.root / "activity.sqlite"
                    )
                )
                start_worker.assert_called_once()

                for name in ("reader", "consumer", "validator", "processor"):
                    releases[name].set()
                    self.assertTrue(completed[name].wait(timeout=2), name)
                self.assertTrue(outer_thread.is_alive())
                self.assertEqual(
                    module._DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                    0.0,
                )

                releases["monitor"].set()
                self.assertTrue(completed["monitor"].wait(timeout=2))
                outer_thread.join(timeout=2)
                self.assertFalse(outer_thread.is_alive())
                self.assertEqual(
                    module._DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                    100.0 + module.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
                )
        finally:
            for release in releases.values():
                release.set()
            for thread in threads.values():
                thread.join(timeout=2)
            if outer_thread is not None:
                outer_thread.join(timeout=2)


    def test_document_worker_cooldown_skips_15_second_loops_then_retries(self) -> None:
        from activity_mvp import daemon as module

        eligible = {
            "allowed": True,
            "reason": "",
            "raw_access": False,
        }
        with patch.object(
            module,
            "_DOCUMENT_CHANGE_WORKER_THREAD",
            None,
        ), patch.object(
            module,
            "_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC",
            900.0,
        ), patch(
            "activity_mvp.daemon.time.monotonic",
            side_effect=(15.0, 30.0, 45.0, 900.0),
        ), patch(
            "activity_mvp.daemon._document_change_worker_startup_eligibility",
            return_value=eligible,
        ) as eligibility, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker",
            return_value=True,
        ) as worker:
            results = [
                _maybe_start_document_change_worker_when_eligible(
                    self.root / "activity.sqlite",
                    self.root,
                )
                for _ in range(4)
            ]

        self.assertEqual(
            [result["reason"] for result in results[:3]],
            ["worker_retry_cooldown"] * 3,
        )
        self.assertTrue(all(not result["started"] for result in results[:3]))
        self.assertTrue(
            all(
                result["retry_cooldown_seconds"]
                == module.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS
                for result in results[:3]
            )
        )
        self.assertTrue(results[3]["started"])
        eligibility.assert_called_once_with(self.root)
        worker.assert_called_once_with(self.root / "activity.sqlite")


    def test_background_maintenance_requires_ac_and_locked_or_ten_minutes_idle(self) -> None:
        locked = {
            "state": "locked",
            "locked": True,
            "on_console": False,
            "capture_allowed": False,
        }
        unlocked = {
            "state": "unlocked",
            "locked": False,
            "on_console": True,
            "capture_allowed": True,
        }
        ac = {"external_power": True}

        locked_result = _background_maintenance_eligibility(
            self.root,
            session=locked,
            power=ac,
            idle={"available": False},
        )
        active_result = _background_maintenance_eligibility(
            self.root,
            session=unlocked,
            power=ac,
            idle={
                "available": True,
                "idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS - 1,
            },
        )
        idle_result = _background_maintenance_eligibility(
            self.root,
            session=unlocked,
            power=ac,
            idle={
                "available": True,
                "idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
            },
        )
        battery_result = _background_maintenance_eligibility(
            self.root,
            session=locked,
            power={"external_power": False},
            idle={"available": True, "idle_seconds": 3600},
        )
        unknown_session_result = _background_maintenance_eligibility(
            self.root,
            session={"state": "unknown"},
            power=ac,
            idle={"available": True, "idle_seconds": 3600},
        )

        self.assertTrue(locked_result["allowed"])
        self.assertEqual(locked_result["session_mode"], "locked")
        self.assertFalse(active_result["allowed"])
        self.assertEqual(active_result["reason"], "idle_required")
        self.assertTrue(idle_result["allowed"])
        self.assertEqual(idle_result["session_mode"], "unlocked")
        self.assertFalse(battery_result["allowed"])
        self.assertEqual(battery_result["reason"], "external_power_required")
        self.assertFalse(unknown_session_result["allowed"])
        self.assertEqual(unknown_session_result["reason"], "session_not_eligible")


    def test_retention_pending_since_persists_until_privacy_deadline_override(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        started_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
        initial_timestamp = started_at.isoformat(timespec="seconds")
        with patch("activity_mvp.daemon.utc_now", return_value=initial_timestamp):
            marker = _mark_daily_maintenance_pending(self.conn, "2026-07-27")
        self.assertEqual(marker["pending_since"], initial_timestamp)

        later_day = (started_at + timedelta(days=1)).isoformat(timespec="seconds")
        with patch("activity_mvp.daemon.utc_now", return_value=later_day):
            next_marker = _mark_daily_maintenance_pending(self.conn, "2026-07-28")
        self.assertEqual(next_marker["pending_since"], initial_timestamp)

        before_deadline = (
            started_at
            + timedelta(
                seconds=module.RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS - 1
            )
        ).isoformat(timespec="seconds")
        with patch("activity_mvp.daemon.utc_now", return_value=before_deadline):
            state = module._daily_maintenance_state_readonly(db_path)
        self.assertFalse(state["deadline_override"])

        at_deadline = (
            started_at
            + timedelta(
                seconds=module.RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS
            )
        ).isoformat(timespec="seconds")
        with patch("activity_mvp.daemon.utc_now", return_value=at_deadline):
            state = module._daily_maintenance_state_readonly(db_path)
        self.assertTrue(state["deadline_override"])
        self.assertEqual(
            state["deferred_seconds"],
            module.RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS,
        )
        self.assertFalse(state["raw_access"])


    def test_retention_handoff_admission_reserves_full_timeout_before_deadline(self) -> None:
        started_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
        with patch(
            "activity_mvp.daemon.utc_now",
            return_value=started_at.isoformat(timespec="seconds"),
        ):
            _mark_daily_maintenance_pending(self.conn, "2026-07-27")

        admitted = retention_handoff_admission(
            self.conn,
            now=started_at
            + timedelta(
                seconds=RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS
                - RETENTION_HANDOFF_DRAIN_SECONDS
                - 1
            ),
        )
        blocked = retention_handoff_admission(
            self.conn,
            now=started_at
            + timedelta(
                seconds=RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS
                - RETENTION_HANDOFF_DRAIN_SECONDS
            ),
        )
        overdue = retention_handoff_admission(
            self.conn,
            now=started_at
            + timedelta(seconds=RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS),
        )

        self.assertEqual(HERMES_HANDOFF_MAX_RUNTIME_SECONDS, 20 * 60)
        self.assertEqual(RETENTION_HANDOFF_RELEASE_GRACE_SECONDS, 60)
        self.assertEqual(
            RETENTION_HANDOFF_DRAIN_SECONDS,
            HERMES_HANDOFF_MAX_RUNTIME_SECONDS
            + RETENTION_HANDOFF_RELEASE_GRACE_SECONDS,
        )
        self.assertTrue(admitted["allowed"])
        self.assertEqual(
            admitted["remaining_seconds"],
            RETENTION_HANDOFF_DRAIN_SECONDS + 1,
        )
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["reason"], "retention_deadline_imminent")
        self.assertEqual(
            blocked["remaining_seconds"],
            RETENTION_HANDOFF_DRAIN_SECONDS,
        )
        self.assertFalse(overdue["allowed"])
        self.assertEqual(overdue["remaining_seconds"], 0)
        self.assertFalse(overdue["raw_access"])


    def test_ha_and_quality_schedulers_close_during_retention_drain_window(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        started_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
        with patch(
            "activity_mvp.daemon.utc_now",
            return_value=started_at.isoformat(timespec="seconds"),
        ):
            _mark_daily_maintenance_pending(self.conn, "2026-07-27")
        drain_starts = started_at + timedelta(
            seconds=RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS
            - RETENTION_HANDOFF_DRAIN_SECONDS
        )

        with patch(
            "activity_mvp.daemon.utc_now",
            return_value=drain_starts.isoformat(timespec="seconds"),
        ), patch.object(
            module,
            "_setting_enabled_readonly",
            return_value=True,
        ), patch.object(
            module,
            "_document_baseline_gate_readonly",
        ) as baseline, patch.object(
            module,
            "_background_maintenance_eligibility",
        ) as eligibility, patch.object(
            module,
            "ensure_ready",
        ) as ready:
            ha = module._maybe_start_ha_automation_worker(db_path)
            quality = module._maybe_start_quality_advice_worker(db_path)

        self.assertEqual(ha["status"], "ha_automation_waiting")
        self.assertEqual(quality["status"], "quality_advice_waiting")
        self.assertEqual(ha["reason"], "retention_deadline_imminent")
        self.assertEqual(quality["reason"], "retention_deadline_imminent")
        self.assertEqual(
            ha["remaining_seconds"],
            RETENTION_HANDOFF_DRAIN_SECONDS,
        )
        self.assertEqual(
            quality["remaining_seconds"],
            RETENTION_HANDOFF_DRAIN_SECONDS,
        )
        baseline.assert_not_called()
        eligibility.assert_not_called()
        ready.assert_not_called()


    def test_ha_and_quality_worker_entries_recheck_retention_admission(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        denied = {
            "allowed": False,
            "reason": "retention_deadline_imminent",
            "deadline_at": "2026-07-28T00:00:00+00:00",
            "remaining_seconds": RETENTION_HANDOFF_DRAIN_SECONDS,
            "raw_access": False,
        }
        with patch.object(
            module,
            "retention_handoff_admission",
            return_value=denied,
        ) as admission, patch.object(
            module,
            "_document_baseline_gate_readonly",
        ) as baseline, patch.object(
            module,
            "_background_maintenance_eligibility",
        ) as eligibility, patch.object(
            module,
            "run_ha_automation_once",
        ) as ha_run, patch.object(
            module,
            "run_quality_advice_once",
        ) as quality_run:
            module._ha_automation_worker(str(db_path))
            module._quality_advice_worker(str(db_path))

        self.assertEqual(admission.call_count, 2)
        baseline.assert_not_called()
        eligibility.assert_not_called()
        ha_run.assert_not_called()
        quality_run.assert_not_called()
        states = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key IN ("
                "'ha_auto_status','quality_advice_status')"
            ).fetchall()
        }
        self.assertEqual(
            states,
            {
                "ha_auto_status": "waiting_for_retention",
                "quality_advice_status": "waiting_for_retention",
            },
        )


    def test_privacy_deadline_override_does_not_probe_session_power_or_idle(self) -> None:
        with patch("activity_mvp.daemon.session_state") as session, patch(
            "activity_mvp.daemon.power_state"
        ) as power, patch(
            "activity_mvp.daemon.system_idle_seconds"
        ) as idle:
            result = _background_maintenance_eligibility(
                self.root,
                privacy_deadline_override=True,
            )

        self.assertTrue(result["allowed"])
        self.assertTrue(result["deadline_override"])
        self.assertEqual(result["reason"], "privacy_deadline_override")
        self.assertFalse(result["raw_access"])
        session.assert_not_called()
        power.assert_not_called()
        idle.assert_not_called()


    def test_document_baseline_gate_fails_closed_for_enabled_zero_roots(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        enabled = module._document_baseline_gate_readonly(db_path)
        self.assertFalse(enabled["allowed"])
        self.assertEqual(enabled["reason"], "document_roots_uninitialized")
        self.assertFalse(enabled["raw_access"])

        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            "updated_at=excluded.updated_at",
            (
                "document_change_tracking_enabled",
                "false",
                "2026-07-27T00:00:00+00:00",
            ),
        )
        self.conn.commit()
        disabled = module._document_baseline_gate_readonly(db_path)
        self.assertTrue(disabled["allowed"])
        self.assertEqual(disabled["reason"], "document_tracking_disabled")
        self.assertFalse(disabled["raw_access"])


    def test_document_baseline_gate_requires_full_coverage_completion(self) -> None:
        from activity_mvp import daemon as module

        incomplete = {
            "coverage": {
                "enabled": True,
                "root_count": 1,
                "coverage_status": "partial",
                "is_complete": False,
            },
            "raw_access": False,
        }
        complete = {
            "coverage": {
                "enabled": True,
                "root_count": 1,
                "coverage_status": "complete",
                "is_complete": True,
            },
            "raw_access": False,
        }
        with patch(
            "activity_mvp.document_changes.document_changes_status",
            side_effect=(incomplete, complete),
        ):
            denied = module._document_baseline_gate_readonly(
                self.root / "activity.sqlite"
            )
            allowed = module._document_baseline_gate_readonly(
                self.root / "activity.sqlite"
            )

        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "document_baseline_incomplete")
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["reason"], "")


    def test_ha_and_quality_workers_use_background_maintenance_gate(self) -> None:
        from activity_mvp import daemon as module

        waiting = {
            "allowed": False,
            "reason": "idle_required",
            "idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS - 1,
            "min_idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
            "raw_access": False,
        }
        with patch.object(
            module,
            "_setting_enabled_readonly",
            return_value=True,
        ), patch.object(
            module,
            "_document_baseline_gate_readonly",
            return_value={"allowed": True, "reason": "", "raw_access": False},
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
            return_value=waiting,
        ) as eligibility, patch.object(
            module,
            "ensure_ready",
        ) as ready:
            ha = module._maybe_start_ha_automation_worker(
                self.root / "activity.sqlite"
            )
            quality = module._maybe_start_quality_advice_worker(
                self.root / "activity.sqlite"
            )

        self.assertEqual(ha["status"], "ha_automation_waiting")
        self.assertEqual(quality["status"], "quality_advice_waiting")
        self.assertEqual(ha["reason"], "idle_required")
        self.assertEqual(quality["reason"], "idle_required")
        self.assertEqual(eligibility.call_count, 2)
        ready.assert_not_called()


    def test_document_worker_waits_while_daily_maintenance_can_start(self) -> None:
        result, maintenance, eligibility, worker = (
            self._start_document_worker_with_pending_daily(
                {"allowed": True, "reason": "", "raw_access": False}
            )
        )

        self.assertFalse(result["allowed"])
        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "daily_maintenance_pending")
        maintenance.assert_called_once()
        eligibility.assert_not_called()
        worker.assert_not_called()


    def test_document_worker_starts_while_daily_maintenance_waits_for_idle(self) -> None:
        # 2026-10-05: a pending writer waiting for ten idle minutes kept file
        # tracking off for a whole day of continuous use.
        result, maintenance, eligibility, worker = (
            self._start_document_worker_with_pending_daily(
                {"allowed": False, "reason": "idle_required", "raw_access": False}
            )
        )

        self.assertTrue(result["started"])
        maintenance.assert_called_once()
        eligibility.assert_called_once()
        worker.assert_called_once()


    def test_document_worker_starts_while_daily_maintenance_is_in_retry_cooldown(self) -> None:
        result, maintenance, eligibility, worker = (
            self._start_document_worker_with_pending_daily(
                {"allowed": True, "reason": "", "raw_access": False},
                retry_not_before=float("inf"),
            )
        )

        self.assertTrue(result["started"])
        maintenance.assert_not_called()
        worker.assert_called_once()


    def test_document_worker_waits_while_daily_maintenance_is_running(self) -> None:
        from activity_mvp import daemon as module

        class ActiveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with patch.object(module, "_DAILY_MAINTENANCE_THREAD", ActiveThread()), patch(
            "activity_mvp.daemon._maybe_start_document_change_worker",
            return_value=True,
        ) as worker:
            result = _maybe_start_document_change_worker_when_eligible(
                self.root / "activity.sqlite",
                self.root,
            )

        self.assertEqual(result["reason"], "daily_maintenance_pending")
        worker.assert_not_called()


    def test_document_worker_waits_when_daily_maintenance_state_is_unreadable(self) -> None:
        from activity_mvp import daemon as module

        with patch.object(module, "_DAILY_MAINTENANCE_THREAD", None), patch.object(
            module,
            "_daily_maintenance_state_readonly",
            return_value={"available": False, "due": False},
        ), patch(
            "activity_mvp.daemon._maybe_start_document_change_worker",
            return_value=True,
        ) as worker:
            result = _maybe_start_document_change_worker_when_eligible(
                self.root / "activity.sqlite",
                self.root,
            )

        self.assertEqual(result["reason"], "daily_maintenance_pending")
        worker.assert_not_called()


    def test_ha_and_quality_wait_for_enabled_document_baselines(self) -> None:
        from activity_mvp import daemon as module

        now = "2026-07-27T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO file_watch_roots("
            "root_id,root_label,enabled,baseline_status,created_at,updated_at"
            ") VALUES(?,?,1,'partial',?,?)",
            ("test-root", "Test", now, now),
        )
        self.conn.commit()

        with patch.object(
            module,
            "_setting_enabled_readonly",
            return_value=True,
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
        ) as eligibility, patch.object(
            module,
            "ensure_ready",
        ) as ready:
            ha = module._maybe_start_ha_automation_worker(
                self.root / "activity.sqlite"
            )
            quality = module._maybe_start_quality_advice_worker(
                self.root / "activity.sqlite"
            )

        self.assertEqual(ha["status"], "ha_automation_waiting")
        self.assertEqual(quality["status"], "quality_advice_waiting")
        self.assertEqual(ha["reason"], "document_baseline_incomplete")
        self.assertEqual(quality["reason"], "document_baseline_incomplete")
        eligibility.assert_not_called()
        ready.assert_not_called()


    def test_daily_waits_for_active_document_baseline_reconcile(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        _mark_daily_maintenance_pending(self.conn, "2026-07-27")

        class ActiveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with patch.object(
            module,
            "_DAILY_MAINTENANCE_THREAD",
            None,
        ), patch.object(
            module,
            "_DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC",
            0.0,
        ), patch.object(
            module,
            "_DOCUMENT_CHANGE_WORKER_THREAD",
            ActiveThread(),
        ), patch.object(
            module,
            "_document_baseline_gate_readonly",
            return_value={
                "allowed": False,
                "reason": "document_baseline_incomplete",
                "raw_access": False,
            },
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
        ) as eligibility, patch(
            "activity_mvp.daemon.threading.Thread"
        ) as thread:
            result = _maybe_start_daily_maintenance_worker(db_path)

        self.assertEqual(result["status"], "daily_maintenance_waiting")
        self.assertEqual(result["reason"], "document_baseline_reconciling")
        self.assertFalse(result["raw_access"])
        self.assertNotIn(str(self.root), json.dumps(result))
        eligibility.assert_not_called()
        thread.assert_not_called()


    def test_privacy_deadline_override_starts_daily_despite_normal_blockers(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"

        class DeferredThread:
            def __init__(self) -> None:
                self.started = False

            def start(self) -> None:
                self.started = True

            def is_alive(self) -> bool:
                return False

        deferred_thread = DeferredThread()
        state = {
            "available": True,
            "due": True,
            "pending_day": "2026-07-27",
            "pending_since": "2026-07-26T00:00:00+00:00",
            "status": "waiting_for_eligibility",
            "deferred_seconds": module.RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS,
            "deadline_override": True,
            "raw_access": False,
        }
        with patch.object(
            module,
            "_DAILY_MAINTENANCE_THREAD",
            None,
        ), patch.object(
            module,
            "_DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC",
            999.0,
        ), patch(
            "activity_mvp.daemon.time.monotonic",
            return_value=0.0,
        ), patch.object(
            module,
            "_daily_maintenance_state_readonly",
            return_value=state,
        ), patch.object(
            module,
            "_document_baseline_reconcile_in_progress",
            return_value={
                "blocked": True,
                "reason": "document_baseline_reconciling",
                "raw_access": False,
            },
        ), patch.object(
            module,
            "_heavy_background_worker_running",
            return_value=True,
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
            wraps=module._background_maintenance_eligibility,
        ) as eligibility, patch(
            "activity_mvp.daemon.threading.Thread",
            return_value=deferred_thread,
        ):
            result = _maybe_start_daily_maintenance_worker(db_path)

        self.assertEqual(result["status"], "daily_maintenance_started")
        self.assertTrue(result["deadline_override"])
        self.assertEqual(result["reason"], "privacy_deadline_override")
        self.assertTrue(deferred_thread.started)
        eligibility.assert_called_once_with(
            db_path.resolve().parent,
            privacy_deadline_override=True,
        )


    def test_daily_maintenance_worker_records_content_free_completion_marker(self) -> None:
        db_path = self.root / "activity.sqlite"
        day = "2026-07-27"
        marker = _mark_daily_maintenance_pending(self.conn, day)
        self.assertEqual(marker["status"], "waiting_for_eligibility")

        with patch(
            "activity_mvp.daemon._safe_cleanup_retention_for_daemon",
            return_value={"status": "applied"},
        ) as retention, patch(
            "activity_mvp.daemon._safe_hygiene_cleanup_for_daemon",
            return_value={"status": "disabled", "applied": False},
        ) as hygiene, patch(
            "activity_mvp.daemon._background_maintenance_eligibility",
            return_value={"allowed": True, "reason": "", "raw_access": False},
        ) as eligibility:
            _daily_maintenance_worker(str(db_path), day)

        state = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key LIKE 'retention_cleanup_%'"
            ).fetchall()
        }
        self.assertEqual(state["retention_cleanup_pending_day"], "")
        self.assertEqual(state["retention_cleanup_last_completed_day"], day)
        self.assertEqual(state["retention_cleanup_status"], "ok")
        self.assertEqual(state["retention_cleanup_last_error"], "")
        self.assertEqual(state["retention_cleanup_pending_since"], "")
        retention.assert_called_once()
        hygiene.assert_called_once()
        eligibility.assert_called_once_with(
            db_path.resolve().parent,
            privacy_deadline_override=False,
        )


    def test_daily_maintenance_worker_preserves_next_day_pending_marker(self) -> None:
        db_path = self.root / "activity.sqlite"
        current_day = "2026-07-27"
        next_day = "2026-07-28"
        _mark_daily_maintenance_pending(self.conn, current_day)

        def retention_crossing_midnight(*_args, **_kwargs):
            next_day_conn = ensure_ready(db_path)
            try:
                _mark_daily_maintenance_pending(next_day_conn, next_day)
            finally:
                next_day_conn.close()
            return {"status": "applied"}

        with patch(
            "activity_mvp.daemon._safe_cleanup_retention_for_daemon",
            side_effect=retention_crossing_midnight,
        ), patch(
            "activity_mvp.daemon._safe_hygiene_cleanup_for_daemon",
            return_value={"status": "disabled", "applied": False},
        ), patch(
            "activity_mvp.daemon._background_maintenance_eligibility",
            return_value={"allowed": True, "reason": "", "raw_access": False},
        ):
            _daily_maintenance_worker(str(db_path), current_day)

        state = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key LIKE 'retention_cleanup_%'"
            ).fetchall()
        }
        self.assertEqual(state["retention_cleanup_pending_day"], next_day)
        self.assertEqual(
            state["retention_cleanup_last_completed_day"],
            current_day,
        )
        self.assertEqual(
            state["retention_cleanup_status"],
            "waiting_for_eligibility",
        )
        self.assertEqual(state["retention_cleanup_last_error"], "")
        self.assertTrue(state["retention_cleanup_pending_since"])


    def test_daily_and_downstream_workers_recheck_entry_eligibility(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        day = "2026-07-27"
        _mark_daily_maintenance_pending(self.conn, day)
        denied = {
            "allowed": False,
            "reason": "external_power_required",
            "raw_access": False,
        }
        with patch.object(
            module,
            "_document_baseline_reconcile_in_progress",
            return_value={"blocked": False, "reason": "", "raw_access": False},
        ), patch.object(
            module,
            "_document_baseline_gate_readonly",
            return_value={"allowed": True, "reason": "", "raw_access": False},
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
            return_value=denied,
        ) as eligibility, patch.object(
            module,
            "_safe_cleanup_retention_for_daemon",
        ) as retention, patch.object(
            module,
            "run_ha_automation_once",
        ) as ha_run, patch.object(
            module,
            "run_quality_advice_once",
        ) as quality_run:
            _daily_maintenance_worker(str(db_path), day)
            module._ha_automation_worker(str(db_path))
            module._quality_advice_worker(str(db_path))

        retention.assert_not_called()
        ha_run.assert_not_called()
        quality_run.assert_not_called()
        self.assertEqual(eligibility.call_count, 3)
        states = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key IN ("
                "'retention_cleanup_status','ha_auto_status',"
                "'quality_advice_status')"
            ).fetchall()
        }
        self.assertEqual(
            states,
            {
                "retention_cleanup_status": "waiting_for_eligibility",
                "ha_auto_status": "waiting_for_eligibility",
                "quality_advice_status": "waiting_for_eligibility",
            },
        )


    def test_workers_recheck_document_baseline_at_entry(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        day = "2026-07-27"
        _mark_daily_maintenance_pending(self.conn, day)
        with patch.object(
            module,
            "_document_baseline_reconcile_in_progress",
            return_value={
                "blocked": True,
                "reason": "document_baseline_reconciling",
                "raw_access": False,
            },
        ), patch.object(
            module,
            "_document_baseline_gate_readonly",
            return_value={
                "allowed": False,
                "reason": "document_baseline_incomplete",
                "raw_access": False,
            },
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
        ) as eligibility, patch.object(
            module,
            "_safe_cleanup_retention_for_daemon",
        ) as retention, patch.object(
            module,
            "run_ha_automation_once",
        ) as ha_run, patch.object(
            module,
            "run_quality_advice_once",
        ) as quality_run:
            _daily_maintenance_worker(str(db_path), day)
            module._ha_automation_worker(str(db_path))
            module._quality_advice_worker(str(db_path))

        retention.assert_not_called()
        ha_run.assert_not_called()
        quality_run.assert_not_called()
        eligibility.assert_not_called()
        states = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key IN ("
                "'retention_cleanup_status','ha_auto_status',"
                "'quality_advice_status')"
            ).fetchall()
        }
        self.assertEqual(
            states,
            {
                "retention_cleanup_status": "waiting_for_document_baseline",
                "ha_auto_status": "waiting_for_document_baseline",
                "quality_advice_status": "waiting_for_document_baseline",
            },
        )


    def test_daily_worker_preserves_privacy_deadline_override_at_entry(self) -> None:
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        day = "2026-07-27"
        started_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
        with patch(
            "activity_mvp.daemon.utc_now",
            return_value=started_at.isoformat(timespec="seconds"),
        ):
            _mark_daily_maintenance_pending(self.conn, day)
        deadline = (
            started_at
            + timedelta(
                seconds=module.RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS
            )
        ).isoformat(timespec="seconds")
        with patch(
            "activity_mvp.daemon.utc_now",
            return_value=deadline,
        ), patch.object(
            module,
            "_document_baseline_reconcile_in_progress",
            return_value={
                "blocked": True,
                "reason": "document_baseline_reconciling",
                "raw_access": False,
            },
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
            wraps=module._background_maintenance_eligibility,
        ) as eligibility, patch.object(
            module,
            "_safe_cleanup_retention_for_daemon",
            return_value={"status": "applied"},
        ) as retention, patch.object(
            module,
            "_safe_hygiene_cleanup_for_daemon",
            return_value={"status": "disabled", "applied": False},
        ):
            _daily_maintenance_worker(str(db_path), day)

        retention.assert_called_once()
        eligibility.assert_called_once_with(
            db_path.resolve().parent,
            privacy_deadline_override=True,
        )
        state = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM daemon_state WHERE key LIKE "
                "'retention_cleanup_%'"
            ).fetchall()
        }
        self.assertEqual(state["retention_cleanup_status"], "ok")
        self.assertEqual(state["retention_cleanup_pending_day"], "")
        self.assertEqual(state["retention_cleanup_pending_since"], "")


    def test_daemon_startup_marks_retention_pending_without_bulk_cleanup(self) -> None:
        db_path = self.root / "activity.sqlite"
        order: list[str] = []

        def defer(*args, **kwargs):
            order.append("defer")
            return {"status": "deferred", "root_count": 0, "raw_access": False}

        with patch(
            "activity_mvp.daemon._safe_cleanup_retention_for_daemon",
        ) as retention, patch(
            "activity_mvp.daemon._safe_hygiene_cleanup_for_daemon",
        ) as hygiene, patch(
            "activity_mvp.document_changes.defer_document_change_startup_reconcile",
            side_effect=defer,
        ) as deferred, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker"
        ) as worker:
            result = _run_daemon_startup_maintenance(
                db_path,
                self.root,
                document_reconcile_enabled=False,
            )

        self.assertEqual(order, ["defer"])
        self.assertEqual(result["retention"]["status"], "waiting_for_eligibility")
        retention.assert_not_called()
        hygiene.assert_not_called()
        self.assertEqual(result["document_changes"]["status"], "deferred")
        self.assertEqual(
            result["document_changes"]["startup_reconcile_status"],
            "disabled",
        )
        deferred.assert_called_once()
        worker.assert_not_called()

        order.clear()

        with patch(
            "activity_mvp.daemon._safe_cleanup_retention_for_daemon",
        ) as retention, patch(
            "activity_mvp.daemon._safe_hygiene_cleanup_for_daemon",
        ) as hygiene, patch(
            "activity_mvp.document_changes.defer_document_change_startup_reconcile",
            side_effect=defer,
        ) as deferred, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker"
        ) as worker:
            result = _run_daemon_startup_maintenance(
                db_path,
                self.root,
                document_reconcile_enabled=True,
            )

        self.assertEqual(order, ["defer"])
        retention.assert_not_called()
        hygiene.assert_not_called()
        self.assertEqual(
            result["document_changes"]["startup_reconcile_status"],
            "waiting_for_eligibility",
        )
        deferred.assert_called_once()
        worker.assert_not_called()


    def test_run_daemon_first_cycle_defers_bulk_maintenance_until_after_capture(self) -> None:
        db_path = self.root / "activity.sqlite"
        order: list[str] = []

        class StopLoop(RuntimeError):
            pass

        def record(name: str, result):
            def call(*args, **kwargs):
                order.append(name)
                return result

            return call

        with patch(
            "activity_mvp.daemon._document_change_startup_reconcile_enabled",
            return_value=True,
        ), patch(
            "activity_mvp.daemon.session_state",
            return_value={"state": "unlocked", "locked": False, "on_console": True},
        ), patch(
            "activity_mvp.daemon._safe_cleanup_retention_for_daemon",
            side_effect=record("retention", {"status": "ok"}),
        ) as retention, patch(
            "activity_mvp.daemon._safe_hygiene_cleanup_for_daemon",
            side_effect=record("hygiene", {"status": "ok"}),
        ) as hygiene, patch(
            "activity_mvp.document_changes.defer_document_change_startup_reconcile",
            side_effect=record(
                "defer",
                {"status": "deferred", "root_count": 0, "raw_access": False},
            ),
        ), patch(
            "activity_mvp.daemon._start_dashboard_thread",
            side_effect=record("dashboard", {"status": "started"}),
        ), patch(
            "activity_mvp.daemon.cleanup_raw_vault"
        ) as raw_cleanup, patch(
            "activity_mvp.daemon.run_once",
            side_effect=record("run_once", {"status": "ready"}),
        ), patch(
            "activity_mvp.daemon._maybe_start_daily_maintenance_worker",
            side_effect=record("maintenance", {"status": "daily_maintenance_waiting"}),
        ), patch(
            "activity_mvp.daemon._maybe_start_document_change_worker_when_eligible",
            side_effect=record(
                "eligibility",
                {"started": False, "allowed": False, "reason": "idle_required"},
            ),
        ) as eligibility, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker"
        ) as document_worker, patch(
            "activity_mvp.daemon._maybe_start_ha_automation_worker",
            side_effect=record("ha", None),
        ), patch(
            "activity_mvp.daemon._maybe_start_quality_advice_worker",
            side_effect=record("quality", None),
        ), patch(
            "activity_mvp.daemon._sleep_between_daemon_cycles",
            side_effect=StopLoop("one cycle complete"),
        ):
            with self.assertRaisesRegex(StopLoop, "one cycle complete"):
                run_daemon(
                    db_path,
                    interval_seconds=5,
                    dev_dashboard=True,
                )

        self.assertEqual(
            order,
            [
                "defer",
                "dashboard",
                "run_once",
                "maintenance",
                "eligibility",
                "ha",
                "quality",
            ],
        )
        retention.assert_not_called()
        hygiene.assert_not_called()
        raw_cleanup.assert_not_called()
        # Keep the lexical runtime root so daemon startup can reject an
        # explicitly supplied root symlink instead of resolving through it.
        eligibility.assert_called_once_with(db_path, db_path.parent)
        document_worker.assert_not_called()


    def test_run_daemon_keeps_document_worker_retry_path_available(self) -> None:
        db_path = self.root / "activity.sqlite"
        today = datetime.now().astimezone().date().isoformat()

        class StopLoop(RuntimeError):
            pass

        with patch(
            "activity_mvp.daemon._document_change_startup_reconcile_enabled",
            return_value=True,
        ), patch(
            "activity_mvp.daemon.session_state",
            return_value={
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
        ), patch(
            "activity_mvp.daemon._run_daemon_startup_maintenance",
            return_value={"retention_day": today},
        ), patch(
            "activity_mvp.daemon.cleanup_raw_vault"
        ), patch(
            "activity_mvp.daemon.run_once",
            return_value={"status": "ready"},
        ) as run_once_mock, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker_when_eligible",
            side_effect=(
                {"started": False, "allowed": False, "reason": "idle_required"},
                {"started": True, "allowed": True, "reason": ""},
                {"started": False, "allowed": False, "reason": "idle_required"},
            ),
        ) as eligibility, patch(
            "activity_mvp.daemon._maybe_start_daily_maintenance_worker"
        ), patch(
            "activity_mvp.daemon._maybe_start_ha_automation_worker"
        ), patch(
            "activity_mvp.daemon._maybe_start_quality_advice_worker"
        ), patch(
            "activity_mvp.daemon._sleep_between_daemon_cycles",
            side_effect=(None, None, StopLoop("three cycles complete")),
        ):
            with self.assertRaisesRegex(StopLoop, "three cycles complete"):
                run_daemon(db_path, interval_seconds=5)

        self.assertEqual(run_once_mock.call_count, 3)
        self.assertEqual(eligibility.call_count, 3)


    def test_run_daemon_screen_capture_disabled_keeps_only_background_workers(self) -> None:
        db_path = self.root / "activity.sqlite"
        configure_screen_capture(self.conn, enabled=False)
        today = datetime.now().astimezone().date().isoformat()

        class StopLoop(RuntimeError):
            pass

        with patch(
            "activity_mvp.daemon._document_change_startup_reconcile_enabled",
            return_value=True,
        ), patch(
            "activity_mvp.daemon.session_state",
            return_value={
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
        ), patch(
            "activity_mvp.daemon._run_daemon_startup_maintenance",
            return_value={"retention_day": today},
        ), patch(
            "activity_mvp.daemon.run_once",
        ) as run_once_mock, patch(
            "activity_mvp.daemon._maybe_start_daily_maintenance_worker"
        ) as maintenance, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker_when_eligible"
        ) as document_worker, patch(
            "activity_mvp.daemon._maybe_start_ha_automation_worker"
        ) as ha_worker, patch(
            "activity_mvp.daemon._maybe_start_quality_advice_worker"
        ) as quality_worker, patch(
            "activity_mvp.daemon._sleep_between_daemon_cycles",
            side_effect=StopLoop("file tracking only cycle complete"),
        ) as sleep:
            with self.assertRaisesRegex(StopLoop, "file tracking only cycle complete"):
                run_daemon(db_path, interval_seconds=15)

        run_once_mock.assert_not_called()
        maintenance.assert_called_once_with(db_path)
        document_worker.assert_called_once_with(db_path, db_path.parent)
        ha_worker.assert_called_once_with(db_path)
        quality_worker.assert_called_once_with(db_path)
        sleep.assert_called_once_with(SCREEN_CAPTURE_DISABLED_POLL_SECONDS, grant_db_path=db_path)
        conn = ensure_ready(db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM daemon_state WHERE key='daemon_status'"
                ).fetchone()["value"],
                "paused",
            )
            beat = json.loads(conn.execute("SELECT value FROM daemon_state WHERE key='daemon_heartbeat'").fetchone()["value"])
            self.assertEqual(beat["pid"], os.getpid())
            self.assertEqual(beat["phase"], "running")
            self.assertLess((datetime.now(timezone.utc) - datetime.fromisoformat(beat["at"])).total_seconds(), 10)
        finally:
            conn.close()


    def test_retention_cleanup_keeps_only_today_and_yesterday_by_default(self) -> None:
        cutoff = retention_cleanup_cutoff()
        old_at = (cutoff - timedelta(minutes=10)).isoformat(timespec="seconds")
        old_end = (cutoff - timedelta(minutes=9)).isoformat(timespec="seconds")
        kept_at = (cutoff + timedelta(hours=1)).isoformat(timespec="seconds")
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": old_at,
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "旧活动",
                    "ocr_text": "应该被今昨保留策略清理删除",
                },
                {
                    "captured_at": kept_at,
                    "display_id": "main",
                    "app_name": "Notes",
                    "window_title": "昨天活动",
                    "ocr_text": "应该继续保留到今天",
                }
            ],
        )
        package = generate_package(self.conn, old_at, old_end, self.root / "packages")["package"]
        preview = cleanup_retention_preview(self.conn, self.root)
        self.assertEqual(preview["mode"], "dry_run")
        self.assertGreaterEqual(preview["would_delete"]["activities"], 1)
        self.assertNotIn("segments", preview["would_delete"])
        self.assertIn("segments", preview["would_mask"])
        self.assertIn("segments", preview["already_masked"])
        self.assertTrue(preview["placeholder_gap_filtered"])
        self.assertEqual(preview["retention_range"]["from"], "0001-01-01T00:00:00+00:00")
        self.assertEqual(len(search(self.conn, "旧活动")), 1)
        with self.assertRaises(ValueError):
            cleanup_retention_preview(self.conn, self.root, days=0)
        with self.assertRaises(ValueError):
            cleanup_retention(self.conn, self.root, days=0)
        cleanup = cleanup_retention(self.conn, self.root)
        self.assertEqual(cleanup["retention_policy"], "today_and_yesterday")
        self.assertEqual(cleanup["mode"], "applied")
        self.assertIn("summary", cleanup)
        self.assertEqual(search(self.conn, "旧活动"), [])
        self.assertEqual(len(search(self.conn, "昨天活动")), 1)
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "expired_deleted")
        self.assertEqual(package["retention_until"], retention_until_iso_for_range_start(old_at))


    def test_safe_daemon_retention_cleanup_records_error_without_marking_daemon_failed(self) -> None:
        with patch("activity_mvp.daemon.cleanup_retention", side_effect=RuntimeError("missing selected column")):
            result = _safe_cleanup_retention_for_daemon(self.conn, self.root)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "retention_cleanup_failed")
        state = {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key, value FROM daemon_state WHERE key IN ('last_error', 'daemon_status')")
        }
        self.assertEqual(state["last_error"], "retention_cleanup_failed")
        self.assertNotIn("missing selected column", json.dumps(result))
        self.assertEqual(state["daemon_status"], "running")


    def test_safe_daemon_hygiene_cleanup_records_error_without_marking_daemon_failed(self) -> None:
        configure_hygiene_cleanup(self.conn, self.root, auto_cleanup=True, backup_rotation=True)
        with patch("activity_mvp.daemon.hygiene_cleanup_report", side_effect=RuntimeError("quarantine busy")):
            result = _safe_hygiene_cleanup_for_daemon(self.conn, self.root, self.root / "activity.sqlite")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "hygiene_cleanup_failed")
        state = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                """
                SELECT key, value
                FROM daemon_state
                WHERE key IN ('hygiene_cleanup_last_error', 'hygiene_cleanup_status', 'daemon_status')
                """
            )
        }
        self.assertEqual(state["hygiene_cleanup_last_error"], "hygiene_cleanup_failed")
        self.assertNotIn("quarantine busy", json.dumps(result))
        self.assertEqual(state["hygiene_cleanup_status"], "error")
        self.assertEqual(state["daemon_status"], "running")


    def test_daemon_failure_codes_never_persist_exception_details(self) -> None:
        secret = "/Users/example/private/client-plan.md content hash deadbeef"
        self.assertEqual(_safe_daemon_failure_code("capture", RuntimeError(secret)), "capture_failed")
        self.assertEqual(
            _safe_daemon_failure_code("capture", sqlite3.OperationalError(f"database is locked at {secret}")),
            "database_busy",
        )


    def test_run_once_sanitizes_uncaught_capture_exception(self) -> None:
        secret = "/Users/example/private/client-plan.md content hash deadbeef"
        unlocked = {"state": "unlocked", "locked": False, "on_console": True}
        with patch("activity_mvp.daemon.session_state", return_value=unlocked), patch(
            "activity_mvp.daemon.capture_sck_keyframe_batch",
            side_effect=RuntimeError(secret),
        ):
            result = run_once(self.root / "activity.sqlite")

        self.assertEqual(result["error"], "capture_failed")
        state = self.conn.execute("SELECT value FROM daemon_state WHERE key = 'last_error'").fetchone()["value"]
        self.assertEqual(state, "capture_failed")
        self.assertNotIn(secret, json.dumps(result))


    def test_run_once_reports_raw_cleanup_failure_without_exception_details(self) -> None:
        secret = "/Volumes/private/client-frame.png content hash deadbeef"
        unlocked = {"state": "unlocked", "locked": False, "on_console": True}
        with patch("activity_mvp.daemon.session_state", return_value=unlocked), patch(
            "activity_mvp.daemon.cleanup_raw_vault",
            side_effect=PermissionError(secret),
        ):
            result = run_once(self.root / "activity.sqlite")

        self.assertEqual(result["error"], "raw_cleanup_failed")
        state = self.conn.execute("SELECT value FROM daemon_state WHERE key = 'last_error'").fetchone()["value"]
        self.assertEqual(state, "raw_cleanup_failed")
        self.assertNotIn(secret, json.dumps(result))


    def test_raw_vault_cleanup_rejects_root_and_vault_symlinks_without_touching_targets(self) -> None:
        outside_root = self.root / "outside-root"
        outside_root.mkdir(mode=0o755)
        outside_root_vault = outside_root / "raw_vault"
        outside_root_vault.mkdir(mode=0o755)
        root_secret = outside_root_vault / "must-survive-root.txt"
        root_secret.write_text("must survive root symlink", encoding="utf-8")
        root_link = self.root / "linked-runtime-root"
        root_link.symlink_to(outside_root, target_is_directory=True)
        before_root_mode = outside_root_vault.stat().st_mode & 0o777

        with self.assertRaises(PermissionError):
            cleanup_raw_vault(root_link)

        self.assertEqual(root_secret.read_text(encoding="utf-8"), "must survive root symlink")
        self.assertEqual(outside_root_vault.stat().st_mode & 0o777, before_root_mode)
        self.assertEqual(count_raw_residue(root_link), 1)

        safe_root = self.root / "safe-runtime-root"
        safe_root.mkdir(mode=0o700)
        outside_vault = self.root / "outside-vault"
        outside_vault.mkdir(mode=0o755)
        vault_secret = outside_vault / "must-survive-vault.txt"
        vault_secret.write_text("must survive raw vault symlink", encoding="utf-8")
        (safe_root / "raw_vault").symlink_to(
            outside_vault,
            target_is_directory=True,
        )
        before_vault_mode = outside_vault.stat().st_mode & 0o777

        with self.assertRaises(PermissionError):
            cleanup_raw_vault(safe_root)

        self.assertEqual(vault_secret.read_text(encoding="utf-8"), "must survive raw vault symlink")
        self.assertEqual(outside_vault.stat().st_mode & 0o777, before_vault_mode)
        self.assertEqual(count_raw_residue(safe_root), 1)


    def test_raw_vault_cleanup_tightens_directory_and_unlinks_child_symlink_only(self) -> None:
        runtime_root = self.root / "normal-runtime-root"
        runtime_root.mkdir(mode=0o755)
        vault = runtime_root / "raw_vault"
        vault.mkdir(mode=0o755)
        residue = vault / "capture.txt"
        residue.write_text("delete me", encoding="utf-8")
        outside = self.root / "outside-child"
        outside.mkdir(mode=0o755)
        must_survive = outside / "must-survive.txt"
        must_survive.write_text("outside target", encoding="utf-8")
        child_link = vault / "linked-child"
        child_link.symlink_to(outside, target_is_directory=True)

        removed = cleanup_raw_vault(runtime_root)

        self.assertEqual(removed, 2)
        self.assertEqual(vault.stat().st_mode & 0o777, 0o700)
        self.assertFalse(residue.exists())
        self.assertFalse(child_link.exists())
        self.assertEqual(must_survive.read_text(encoding="utf-8"), "outside target")
        self.assertEqual(count_raw_residue(runtime_root), 0)


    def test_run_once_and_status_fail_closed_for_unsafe_raw_vault(self) -> None:
        outside = self.root / "unsafe-vault-target"
        outside.mkdir(mode=0o755)
        must_survive = outside / "must-survive.txt"
        must_survive.write_text("outside target", encoding="utf-8")
        (self.root / "raw_vault").symlink_to(outside, target_is_directory=True)

        result = run_once(self.root / "activity.sqlite")
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)

        self.assertEqual(result["error"], "raw_cleanup_failed")
        self.assertEqual(status["raw_residue_count"], 1)
        self.assertEqual(must_survive.read_text(encoding="utf-8"), "outside target")
        self.assertIn(
            "raw_residue",
            {item["code"] for item in status["attention_items"]},
        )


    def test_daemon_hygiene_cleanup_does_not_delete_export_cache_without_manual_apply(self) -> None:
        fake_home = self.root / "home"
        live_core = fake_home / "Library/Application Support/Personal AI Monitor/core"
        live_db = live_core / "data/activity.sqlite"
        live_conn = ensure_ready(live_db)
        try:
            configure_hygiene_cleanup(live_conn, live_core, auto_cleanup=True, backup_rotation=True)
            exports = live_core / "data/exports"
            exports.mkdir(parents=True)
            stale_export = exports / "live-check-cleaned.md"
            stale_export.write_text("# stale cleaned export", encoding="utf-8")
            stale_time = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp()
            os.utime(stale_export, (stale_time, stale_time))
            with patch("activity_mvp.data_hygiene.Path.home", return_value=fake_home):
                result = _safe_hygiene_cleanup_for_daemon(live_conn, live_core, live_db)
        finally:
            live_conn.close()

        self.assertTrue(stale_export.exists())
        self.assertFalse(result["activity_export_cache_auto_cleanup_enabled"])
        self.assertFalse(result["policy"]["activity_export_cache_auto_cleanup_enabled"])
        self.assertEqual(result["activity_export_cache"]["candidate_count"], 0)
        self.assertEqual(result["activity_export_cache"]["deleted_count"], 0)
        self.assertFalse(result["activity_export_cache"]["auto_cleanup_enabled"])
        self.assertTrue(result["activity_export_cache"]["manual_apply_required"])


    def test_configure_hygiene_cleanup_marks_enabled_status_ready(self) -> None:
        result = configure_hygiene_cleanup(self.conn, self.root, auto_cleanup=True, backup_rotation=True)

        self.assertTrue(result["enabled"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["label"], "已启用")


    def test_cleanup_legacy_server_pid_removes_stale_file(self) -> None:
        pid_path = self.root / "server.pid"
        pid_path.write_text("78916\n", encoding="utf-8")

        self.assertTrue(cleanup_legacy_server_pid(self.root))
        self.assertFalse(pid_path.exists())
        self.assertFalse(cleanup_legacy_server_pid(self.root))


    def test_reclassify_sanitizes_existing_communication_activity(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T12:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Feishu",
                    "window_title": "客户A",
                    "ocr_text": "沟通内容",
                }
            ],
        )
        activity_id = timeline(self.conn)[0]["id"]
        self.conn.execute(
            "UPDATE extracted_activities SET activity_summary = ? WHERE id = ?",
            ("Feishu：客户A 私聊正文", activity_id),
        )
        self.conn.execute(
            "UPDATE search_index SET summary = ?, keywords = ? WHERE entity_id = ?",
            ("Feishu：客户A 私聊正文", "客户A 私聊正文", activity_id),
        )
        self.conn.commit()
        package = generate_package(
            self.conn,
            "2026-05-22T12:00:00+08:00",
            "2026-05-22T12:05:00+08:00",
            self.root / "packages",
        )["package"]
        self.assertEqual(len(search(self.conn, "私聊正文")), 1)
        result = reclassify_existing(self.conn)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(search(self.conn, "私聊正文"), [])
        self.assertIn("未保留对话原文", timeline(self.conn)[0]["activity_summary"])
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "revoked")


    def test_reclassify_does_not_duplicate_normal_summary_prefix(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T12:30:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "项目记录",
                    "ocr_text": "普通内容",
                }
            ],
        )
        before = timeline(self.conn)[0]["activity_summary"]
        self.assertEqual(before, "Obsidian：普通内容")
        result = reclassify_existing(self.conn)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(timeline(self.conn)[0]["activity_summary"], "Obsidian：普通内容")


    def test_reclassify_removes_existing_adult_activity_and_revokes_package(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T12:40:00+08:00",
                    "display_id": "main",
                    "app_name": "Chrome",
                    "window_title": "普通页面",
                    "ocr_text": "普通内容",
                }
            ],
        )
        activity_id = timeline(self.conn)[0]["id"]
        package = generate_package(
            self.conn,
            "2026-05-22T12:40:00+08:00",
            "2026-05-22T12:45:00+08:00",
            self.root / "packages",
        )["package"]
        self.conn.execute(
            "UPDATE extracted_activities SET activity_summary = ?, apps = ?, domains = ? WHERE id = ?",
            (
                "Chrome / pornhub.com：Pornhub PRIVATE_RECLASSIFY_TEXT",
                json.dumps(["Chrome"], ensure_ascii=False),
                json.dumps(["pornhub.com"], ensure_ascii=False),
                activity_id,
            ),
        )
        self.conn.execute(
            "UPDATE search_index SET domain = ?, summary = ?, keywords = ? WHERE entity_id = ?",
            (
                "pornhub.com",
                "Chrome / pornhub.com：Pornhub PRIVATE_RECLASSIFY_TEXT",
                "Pornhub PRIVATE_RECLASSIFY_TEXT",
                activity_id,
            ),
        )
        self.conn.commit()

        dry_run = reclassify_existing(self.conn, dry_run=True)
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["blocked"], 1)
        self.assertEqual(dry_run["adult_policy"]["policy_version"], "adult-policy-0.4.0")
        self.assertEqual(len(timeline(self.conn)), 1)
        self.assertNotEqual(search(self.conn, "PRIVATE_RECLASSIFY_TEXT"), [])
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "active")

        result = reclassify_existing(self.conn)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(timeline(self.conn), [])
        self.assertEqual(search(self.conn, "PRIVATE_RECLASSIFY_TEXT"), [])
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "revoked")
        privacy = self.conn.execute(
            "SELECT reason, app_name, domain FROM privacy_events"
        ).fetchone()
        self.assertEqual(privacy["reason"], "sensitive_content")
        self.assertEqual(privacy["app_name"], "")
        self.assertEqual(privacy["domain"], "")


    def test_reclassify_dry_run_reports_personal_medical_hits_without_raw_text(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-06-14T10:00:00+08:00",
                    "display_id": "main",
                    "app_name": "ChatGPT",
                    "window_title": "普通页面",
                    "ocr_text": "普通内容",
                }
            ],
        )
        activity = timeline(self.conn)[0]
        package = generate_package(
            self.conn,
            "2026-06-14T09:59:00+08:00",
            "2026-06-14T10:05:00+08:00",
            self.root / "packages",
        )["package"]
        medical_text = "ChatGPT：我的体检报告里血常规异常，医生建议复诊并讨论用药。"
        self.conn.execute(
            "UPDATE extracted_activities SET activity_summary = ?, apps = ?, domains = ? WHERE id = ?",
            (
                medical_text,
                json.dumps(["ChatGPT"], ensure_ascii=False),
                json.dumps(["chatgpt.com"], ensure_ascii=False),
                activity["id"],
            ),
        )
        self.conn.execute(
            "UPDATE frame_signals SET ocr_redacted_summary = ?, app_name = ?, domain = ? WHERE deletion_group_id = ?",
            (medical_text, "ChatGPT", "chatgpt.com", activity["deletion_group_id"]),
        )
        self.conn.execute(
            "UPDATE search_index SET app_name = ?, domain = ?, summary = ?, keywords = ? WHERE entity_id = ?",
            ("ChatGPT", "chatgpt.com", medical_text, medical_text, activity["id"]),
        )
        self.conn.commit()

        dry_run = reclassify_existing(self.conn, dry_run=True)
        encoded = json.dumps(dry_run, ensure_ascii=False)
        self.assertTrue(dry_run["dry_run"])
        self.assertFalse(dry_run["destructive_actions"])
        self.assertEqual(dry_run["blocked"], 1)
        self.assertEqual(dry_run["dry_run_findings"][0]["reason"], "personal_medical_content")
        self.assertEqual(dry_run["dry_run_findings"][0]["old_app"], "ChatGPT")
        self.assertIn(package["package_id"], dry_run["dry_run_findings"][0]["packages_to_revoke"])
        self.assertNotIn("血常规", encoded)
        self.assertNotIn("用药", encoded)
        self.assertEqual(len(timeline(self.conn)), 1)
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "active")


    def test_reclassify_redacts_existing_frame_signal_content(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-22T12:50:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "隐私规则讨论",
                    "ocr_text": "普通讨论",
                }
            ],
        )
        activity_id = timeline(self.conn)[0]["id"]
        row = self.conn.execute(
            "SELECT deletion_group_id FROM extracted_activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        self.conn.execute(
            """
            UPDATE frame_signals
            SET ocr_redacted_summary = ?, safe_keywords = ?
            WHERE deletion_group_id = ?
            """,
            (
                "讨论 Pornhub 和 adult_content 风险标签应该被清洗",
                "Pornhub adult_content",
                row["deletion_group_id"],
            ),
        )
        self.conn.commit()
        before = cleaned_timeline_export(
            self.conn,
            "2026-05-22T12:49:00+08:00",
            "2026-05-22T12:51:00+08:00",
        )
        self.assertIn("Pornhub", json.dumps(before, ensure_ascii=False))

        result = reclassify_existing(self.conn)
        self.assertEqual(result["updated"], 1)
        after = cleaned_timeline_export(
            self.conn,
            "2026-05-22T12:49:00+08:00",
            "2026-05-22T12:51:00+08:00",
        )
        encoded = json.dumps(after, ensure_ascii=False)
        self.assertNotIn("Pornhub", encoded)
        self.assertNotIn("adult_content", encoded)
        self.assertEqual(timeline(self.conn)[0]["privacy_level"], "partial_redacted")


    def test_schema_defaults_to_raw_refs_not_deleted(self) -> None:
        columns = self.conn.execute("PRAGMA table_info(frame_signals)").fetchall()
        raw_column = next(column for column in columns if column["name"] == "raw_ref_deleted")
        self.assertEqual(str(raw_column["dflt_value"]).strip("'\""), "0")


    def test_sqlite_files_are_private(self) -> None:
        db_path = self.root / "activity.sqlite"
        self.conn.execute("CREATE TABLE IF NOT EXISTS permission_probe(id INTEGER)")
        self.conn.execute("INSERT INTO permission_probe(id) VALUES(1)")
        self.conn.commit()
        harden_sqlite_permissions(db_path)

        self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)


    def test_delete_range_removes_coverage_ledger_for_range(self) -> None:
        ingest_events(self.conn, EVENTS)
        before = self.conn.execute("SELECT COUNT(*) AS count FROM capture_coverage_ledger").fetchone()["count"]
        self.assertGreater(before, 0)
        delete_range(
            self.conn,
            "2026-05-22T08:59:00+08:00",
            "2026-05-22T09:12:00+08:00",
        )
        after = self.conn.execute("SELECT COUNT(*) AS count FROM capture_coverage_ledger").fetchone()["count"]
        self.assertEqual(after, 0)


    def test_dashboard_origin_stays_local_only(self) -> None:
        self.assertFalse(_origin_allowed(None, "127.0.0.1", 8765))
        self.assertTrue(_origin_allowed("http://127.0.0.1:8765", "127.0.0.1", 8765))
        self.assertTrue(_origin_allowed("http://127.0.0.1:8765/api/status", "127.0.0.1", 8765))
        self.assertTrue(_origin_allowed("http://localhost:8765", "127.0.0.1", 8765))
        self.assertTrue(_origin_allowed("http://[::1]:8765", "127.0.0.1", 8765))
        self.assertFalse(_origin_allowed("https://example.com", "127.0.0.1", 8765))


    def test_dashboard_host_header_stays_loopback_only(self) -> None:
        self.assertTrue(_host_allowed("127.0.0.1:8765", "127.0.0.1", 8765))
        self.assertTrue(_host_allowed("localhost:8765", "127.0.0.1", 8765))
        self.assertTrue(_host_allowed("[::1]:8765", "127.0.0.1", 8765))
        self.assertFalse(_host_allowed("example.com:8765", "127.0.0.1", 8765))
        self.assertFalse(_host_allowed("127.0.0.1:9999", "127.0.0.1", 8765))
        self.assertFalse(_host_allowed(None, "127.0.0.1", 8765))


    def test_dashboard_rejects_non_loopback_bind_host(self) -> None:
        self.assertEqual(validate_loopback_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback_bind_host("localhost"), "localhost")
        with self.assertRaises(ValueError):
            validate_loopback_bind_host("0.0.0.0")
        with self.assertRaises(ValueError):
            validate_loopback_bind_host("192.168.1.20")


    def test_dashboard_frontend_uses_delete_confirmation_code(self) -> None:
        html = render_dashboard()
        self.assertIn("/api/delete-confirmation", html)
        self.assertIn("confirmation_code", html)
        self.assertLess(html.index("/api/delete-confirmation"), html.index("/api/delete', { ...subject"))


    def test_dev_dashboard_disabled_by_default_in_status(self) -> None:
        self.seed_runtime_progress()
        last_capture_at = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_capture_at", last_capture_at, last_capture_at),
        )
        self.conn.commit()
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={
                "loaded": True,
                "running": True,
                "pid": "12345",
                "last_exit_code": "(never exited)",
            },
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)
        self.assertFalse(status["dev_dashboard_enabled"])
        self.assertEqual(status["launch_agent_label"], "app.personalai.monitor.core")
        self.assertIsInstance(status["launch_agent_loaded"], bool)
        self.assertTrue(status["launch_agent_running"])
        self.assertEqual(status["launch_agent_pid"], "12345")
        self.assertIsInstance(status["next_capture_in_seconds"], int)
        self.assertGreaterEqual(status["next_capture_in_seconds"], 0)
        self.assertLessEqual(status["next_capture_in_seconds"], status["capture_interval_seconds"])
        self.assertIn("runtime_identity", status)
        self.assertEqual(
            Path(status["runtime_identity"]["database_path"]).resolve(),
            (self.root / "activity.sqlite").resolve(),
        )
        self.assertTrue(status["runtime_identity"]["python_executable"])
        self.assertIn(status["runtime_identity"]["path_kind"], {"application_support", "wrapper", "repo_local", "other"})
        self.assertIn("dashboard_health", status)
        self.assertEqual(status["dashboard_health"]["status"], "off")
        self.assertFalse(status["dashboard_health"]["listening"])
        self.assertIn("capture_freshness", status)
        self.assertFalse(status["capture_freshness"]["is_stale"])
        self.assertNotIn("vlm_health", status)
        self.assertNotIn("vlm_backlog", status)


    def test_status_separates_paused_state_from_stopped_service(self) -> None:
        old_capture_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_capture_at", old_capture_at, old_capture_at),
        )
        pause_result = pause(self.conn, mode="today")

        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": False, "running": False, "pid": "", "last_exit_code": ""},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)

        self.assertEqual(pause_result["pause_mode"], "today")
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["pause_mode"], "today")
        self.assertFalse(status["launch_agent_loaded"])
        self.assertFalse(status["launch_agent_running"])
        self.assertFalse(status["capture_freshness"]["is_stale"])
        self.assertFalse(status["capture_freshness"]["active_capture_expected"])
        self.assertEqual(status["capture_freshness"]["reason"], "service_not_running")
        self.assertIn("记录服务未启动", status["capture_freshness"]["summary"])
        self.assertIn("后台已停止", status["status_explanation"])


    def test_status_flags_orphan_dashboard_listener(self) -> None:
        last_capture_at = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_capture_at", last_capture_at, last_capture_at),
        )
        self.conn.commit()
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": True, "pid": "99999", "command": "Python"},
        ):
            status = get_status(self.conn, self.root)

        self.assertEqual(status["dashboard_health"]["status"], "orphan_listener")
        self.assertFalse(status["dashboard_health"]["managed_by_daemon"])
        self.assertTrue(status["dashboard_health"]["orphan_listener"])
        codes = {item["code"] for item in status["attention_items"]}
        self.assertIn("orphan_dashboard_listener", codes)


    def test_status_flags_stale_capture_when_service_runs_without_updates(self) -> None:
        self.seed_runtime_progress()
        old_capture_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_capture_at", old_capture_at, old_capture_at),
        )
        self.conn.commit()
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)

        self.assertTrue(status["capture_freshness"]["is_stale"])
        self.assertEqual(status["capture_freshness"]["reason"], "stale_capture")
        self.assertIn("没有新采集", status["status_explanation"])
        codes = {item["code"] for item in status["attention_items"]}
        self.assertIn("stale_capture", codes)


    def test_status_includes_coverage_health_and_attention_items(self) -> None:
        now = utc_now()
        record_capture_coverage(
            self.conn,
            "main",
            now,
            "keyframe",
            clear=1,
            frames_sampled=1,
            frames_dropped=0,
        )
        record_capture_coverage(
            self.conn,
            "main",
            now,
            "keyframe_duplicate_skip",
            frames_sampled=1,
            frames_dropped=1,
            raw_jobs_created=0,
            raw_jobs_deleted=0,
            duplicate_exact=1,
            degraded_reason="duplicate_frame",
        )
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("last_error", "screen recording permission denied", now),
        )
        residue = self.root / "raw_vault" / "leftover.tmp"
        residue.parent.mkdir(parents=True, exist_ok=True)
        residue.write_text("temporary", encoding="utf-8")
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": False, "pid": "", "last_exit_code": "1"},
        ):
            status = get_status(self.conn, self.root)

        self.assertIn("coverage_health", status)
        self.assertEqual(status["coverage_health"]["frames_sampled"], 2)
        self.assertEqual(status["coverage_health"]["frames_dropped"], 1)
        self.assertEqual(status["coverage_health"]["duplicate_exact_count"], 1)
        self.assertEqual(status["coverage_health"]["display_count"], 1)
        self.assertEqual(status["coverage_health"]["displays"][0]["display_id"], "main")
        self.assertTrue(status["coverage_health"]["displays"][0]["last_capture_at"])
        self.assertIn("画面重复", status["coverage_health"]["main_reasons"][0]["label"])
        codes = {item["code"] for item in status["attention_items"]}
        self.assertIn("last_error", codes)
        self.assertIn("launch_agent_not_running", codes)
        self.assertIn("raw_residue", codes)
        self.assertIn("后台已停止", status["status_explanation"])


    def test_status_splits_active_gap_from_coverage_ledger_gap(self) -> None:
        now = utc_now()
        record_capture_coverage(
            self.conn,
            "main",
            now,
            "keyframe",
            frames_sampled=1,
            frames_dropped=1,
            gap=1,
            degraded_reason="screen_capture_unavailable",
        )
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)

        self.assertEqual(status["today_gap_count"], 0)
        coverage = status["coverage_health"]
        self.assertEqual(coverage["active_gap_count"], 0)
        self.assertEqual(coverage["coverage_ledger_gap_count"], 1)
        self.assertEqual(coverage["coverage_only_gap_count"], 1)
        self.assertEqual(coverage["gap_count_source"], "coverage_ledger_only")
        self.assertEqual(status["privacy_health"]["active_gap_count"], 0)
        self.assertEqual(status["privacy_health"]["coverage_ledger_gap_count"], 1)


    def test_diagnostics_status_is_safe_summary_only(self) -> None:
        now = utc_now()
        sensitive_home = "/Users/" + "exampleuser"
        sensitive_token = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        self.conn.execute(
            """
            INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (
                "last_error",
                f"{sensitive_home}/private/activity.sqlite failed with {sensitive_token}",
                now,
            ),
        )
        self.conn.commit()

        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": False, "pid": "", "last_exit_code": "1"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            report = diagnostics_status(self.conn, self.root, self.root / "activity.sqlite")

        self.assertEqual(report["interface"], "diagnostics")
        self.assertEqual(report["kind"], "danger")
        self.assertFalse(report["raw_access"])
        self.assertFalse(report["destructive_actions"])
        self.assertFalse(report["memory_write_actions"])
        self.assertFalse(report["rule_write_actions"])
        probe_names = {probe["name"] for probe in report["probes"]}
        self.assertIn("core_status", probe_names)
        self.assertIn("ai_handoff", probe_names)
        self.assertIn("privacy_policy", probe_names)
        self.assertIn("hygiene", probe_names)
        self.assertIn("actions", report)
        self.assertIn("action_counts", report)
        self.assertGreaterEqual(report["action_counts"]["read_only"], 1)
        self.assertTrue({item["kind"] for item in report["actions"]}.issubset({"read_only", "previewable", "confirm_required"}))

        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(sensitive_home, encoded)
        self.assertNotIn(sensitive_token, encoded)
        self.assertNotIn("database_path", encoded)
        self.assertNotIn("python_executable", encoded)

        markdown = render_diagnostics_markdown(report)
        self.assertIn("# PAM 本机诊断包", markdown)
        self.assertNotIn(sensitive_home, markdown)
        self.assertNotIn(sensitive_token, markdown)


    def test_diagnostics_quick_mode_skips_slow_full_probes(self) -> None:
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ), patch(
            "activity_mvp.diagnostics.backup_hygiene_report",
            side_effect=AssertionError("quick diagnostics must not run full hygiene"),
        ):
            report = diagnostics_status(self.conn, self.root, self.root / "activity.sqlite", mode="quick")

        self.assertEqual(report["mode"], "quick")
        self.assertLessEqual(report["target_response_ms"], 2000)
        self.assertIn("elapsed_ms", report)
        probes = {probe["name"]: probe for probe in report["probes"]}
        self.assertEqual(probes["hygiene"]["status"], "skipped_quick_mode")
        self.assertEqual(probes["ai_handoff"]["status"], "skipped_quick_mode")
        self.assertFalse(report["raw_access"])


    def test_status_splits_structured_signal_counts(self) -> None:
        now_dt = datetime.now(timezone.utc)
        ext_signal = write_browser_signal(
            self.root,
            {
                "browser": "Dia",
                "bundle_id": "company.thebrowser.dia",
                "url": "https://research.example/path?token=hidden",
            },
        )
        md_path = self.root / "today-document.md"
        md_path.write_text("Today semantic document match text for status.", encoding="utf-8")
        semantic_event = {
            "captured_at": (now_dt + timedelta(seconds=2)).isoformat(timespec="seconds"),
            "display_id": "main",
            "app_name": "Obsidian",
            "window_title": "today-document.md",
            "ocr_text": "Today semantic document match text",
            "document_path": str(md_path),
            "context_confidence": 0.9,
        }
        semantic_event["document_signal"] = build_document_signal(self.root, semantic_event)
        pdf_path = self.root / "today-document.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        metadata_event = {
            "captured_at": (now_dt + timedelta(seconds=3)).isoformat(timespec="seconds"),
            "display_id": "main",
            "app_name": "Preview",
            "window_title": "today-document.pdf",
            "ocr_text": "Today metadata only PDF screen text",
            "document_path": str(pdf_path),
            "context_confidence": 0.9,
        }
        metadata_event["document_signal"] = build_document_signal(self.root, metadata_event)
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": now_dt.isoformat(timespec="seconds"),
                    "display_id": "main",
                    "app_name": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "window_title": "Research",
                    "url": ext_signal["url_redacted"],
                    "domain": ext_signal["domain"],
                    "url_source": "chromium_extension_native_host",
                    "browser_signal": ext_signal,
                    "ocr_text": "Extension browser status count",
                    "context_confidence": 0.9,
                },
                {
                    "captured_at": (now_dt + timedelta(seconds=1)).isoformat(timespec="seconds"),
                    "display_id": "main",
                    "app_name": "Google Chrome",
                    "bundle_id": "com.google.Chrome",
                    "window_title": "Fallback",
                    "url": "https://fallback.example/[redacted]",
                    "domain": "fallback.example",
                    "url_source": "applescript_chrome",
                    "ocr_text": "AppleScript browser status count",
                    "context_confidence": 0.8,
                },
                semantic_event,
                metadata_event,
            ],
        )

        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "12345", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)

        health = status["structured_signal_health"]
        self.assertEqual(health["browser_signal_count"], 2)
        self.assertEqual(health["extension_browser_signal_count"], 1)
        self.assertEqual(health["applescript_browser_signal_count"], 1)
        self.assertTrue(health["latest_extension_signal_active"])
        self.assertEqual(health["document_signal_count"], 2)
        self.assertEqual(health["document_semantic_signal_count"], 1)
        self.assertEqual(health["document_metadata_only_count"], 1)
        self.assertEqual(health["document_signal_status"], "matched")
        self.assertEqual(health["document_signal_reason"], "semantic_match")
        self.assertIn("语义匹配", health["document_signal_diagnostic"]["summary"])


    def test_cli_search_respects_time_range(self) -> None:
        ingest_events(self.conn, EVENTS)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "activity_mvp",
                "--db",
                str(self.root / "activity.sqlite"),
                "search",
                "--query-stdin",
                "--from",
                "2026-05-22T09:02:00+08:00",
                "--to",
                "2026-05-22T09:04:00+08:00",
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            input=json.dumps({"query": "Personal AI Monitor"}),
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [])


    def test_cli_cleanup_retention_defaults_to_dry_run_and_requires_confirmation(self) -> None:
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-05-20T09:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "cleanup cli old",
                    "ocr_text": "cleanup cli old activity",
                    "context_confidence": 0.9,
                }
            ],
        )
        core_cwd = Path(__file__).resolve().parents[1]
        base = [sys.executable, "-m", "activity_mvp", "--db", str(self.root / "activity.sqlite"), "cleanup-retention"]
        dry_run = subprocess.run(
            base,
            cwd=core_cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
        self.assertEqual(json.loads(dry_run.stdout)["mode"], "dry_run")
        self.assertEqual(len(search(self.conn, "cleanup cli old")), 1)

        missing_confirmation = subprocess.run(
            [*base, "--apply"],
            cwd=core_cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(missing_confirmation.returncode, 0)
        self.assertIn("DELETE_EXPIRED_LOCAL_ACTIVITY", missing_confirmation.stderr)
        self.assertEqual(len(search(self.conn, "cleanup cli old")), 1)

        zero_days = subprocess.run(
            [*base, "--days", "0"],
            cwd=core_cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(zero_days.returncode, 0)
        self.assertIn("--days must be at least 1", zero_days.stderr)


    def test_cli_hygiene_cleanup_defaults_to_dry_run_and_requires_confirmation(self) -> None:
        core_cwd = Path(__file__).resolve().parents[1]
        base = [sys.executable, "-m", "activity_mvp", "--db", str(self.root / "activity.sqlite"), "hygiene-cleanup"]
        dry_run = subprocess.run(
            base,
            cwd=core_cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
        self.assertEqual(json.loads(dry_run.stdout)["mode"], "dry_run")

        missing_confirmation = subprocess.run(
            [*base, "--apply"],
            cwd=core_cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(missing_confirmation.returncode, 0)
        self.assertIn(HYGIENE_CLEANUP_CONFIRMATION, missing_confirmation.stderr)


    def test_package_validation_rejects_missing_security_fields(self) -> None:
        result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )
        package = dict(result["package"])
        package.pop("privacy_firewall_version")

        with self.assertRaises(ValueError):
            validate_package(package)


    def test_get_package_revokes_invalid_cached_package(self) -> None:
        ingest_events(self.conn, EVENTS)
        result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )
        package_id = result["package"]["package_id"]
        json_path = Path(result["json_path"])
        cached = json.loads(json_path.read_text(encoding="utf-8"))
        cached.pop("redaction_policy_version")
        json_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

        response = get_package(self.conn, package_id)

        self.assertEqual(response["source_status"], "revoked")
        self.assertEqual(response["reason"], "package_validation_failed")


    def test_list_packages_revokes_active_package_with_missing_cache(self) -> None:
        ingest_events(self.conn, EVENTS)
        result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )
        package_id = result["package"]["package_id"]
        Path(result["json_path"]).unlink()

        rows = list_packages(self.conn)

        row = next(item for item in rows if item["package_id"] == package_id)
        self.assertNotIn("cache_ref", row)
        self.assertEqual(row["source_status"], "revoked")
        self.assertEqual(row["reason"], "cache_missing")
        self.assertFalse(row["cache_available"])
        self.assertEqual(get_package(self.conn, package_id)["source_status"], "revoked")


    def test_revoke_package_removes_cache_without_deleting_activities(self) -> None:
        ingest_events(self.conn, EVENTS)
        result = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )
        package_id = result["package"]["package_id"]
        json_path = Path(result["json_path"])
        markdown_path = Path(result["markdown_path"])

        revoked = revoke_package(self.conn, package_id, "manual_sensitive_review")

        self.assertTrue(revoked["revoked"])
        self.assertEqual(get_package(self.conn, package_id)["source_status"], "revoked")
        self.assertFalse(json_path.exists())
        self.assertFalse(markdown_path.exists())
        self.assertEqual(len(timeline(self.conn)), 1)
        audit = self.conn.execute(
            "SELECT COUNT(*) AS count FROM audit_logs WHERE action = 'revoke_ha_source_package'"
        ).fetchone()["count"]
        self.assertEqual(audit, 1)


    def test_activity_session_opens_closes_and_daemon_gap_closes_stale_session(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T10:00:00+08:00",
            daemon_start=True,
        )
        first_id = opened["current_session"]["id"]

        closed = observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at="2026-07-04T10:05:00+08:00",
        )

        self.assertEqual(closed["closed_session_id"], first_id)
        first = show_activity_session(self.conn, first_id)["session"]
        self.assertEqual(first["status"], "closed")
        self.assertEqual(first["end_reason"], "lock_detected")
        self.assertEqual(first["duration_seconds"], 300)

        second = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T10:06:00+08:00",
        )
        second_id = second["current_session"]["id"]
        restarted = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T10:10:00+08:00",
            daemon_start=True,
        )

        self.assertNotEqual(restarted["current_session"]["id"], second_id)
        self.assertEqual(restarted["closed_session_id"], second_id)
        stale = show_activity_session(self.conn, second_id)["session"]
        self.assertEqual(stale["status"], "closed")
        self.assertEqual(stale["end_reason"], "daemon_gap_closed")
        self.assertEqual(stale["duration_seconds"], 240)


    def test_activity_session_time_slices_cap_gaps_and_keep_safe_output(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T10:00:00+08:00",
        )
        session_id = opened["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-04T10:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "bundle_id": "com.openai.codex",
                    "window_title": "Activity session implementation",
                    "ocr_text": "editing safe local tests",
                }
            ],
        )

        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at="2026-07-04T10:10:00+08:00",
        )

        session = show_activity_session(self.conn, session_id)["session"]
        self.assertEqual(session["duration_seconds"], 600)
        self.assertLess(session["coverage_ratio"], 0.2)
        primary = {item["label"]: item for item in session["primary_focus_share"]}
        self.assertGreater(primary["未记录缺口"]["duration_seconds"], 500)
        self.assertLessEqual(primary["工作 / AI 协作"]["duration_seconds"], 45)
        markdown = render_activity_sessions_markdown(show_activity_session(self.conn, session_id))
        self.assertIn("系统屏幕使用时间明细：未使用", markdown)
        self.assertNotIn("ocr_text", markdown)
        self.assertNotIn("/Users/", markdown)
        self.assertNotIn("https://", markdown)


    def test_activity_session_multi_display_visible_share_and_trusted_domains(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T11:00:00+08:00",
        )
        session_id = opened["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-04T11:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "window_title": "ChatGPT project planning follow up",
                    "url": "https://chatgpt.com/c/project",
                    "domain": "chatgpt.com",
                    "ocr_text": "project planning follow up notes",
                    "screen_region_signal": {"region_kind": "main_content", "actionability": "actionable", "confidence": 0.9},
                },
                {
                    "captured_at": "2026-07-04T11:00:00+08:00",
                    "display_id": "external",
                    "app_name": "IINA",
                    "bundle_id": "com.colliderli.iina",
                    "window_title": "Movie",
                    "ocr_text": "movie player controls",
                    "screen_region_signal": {"region_kind": "media_chrome", "actionability": "noise", "confidence": 0.7},
                },
                {
                    "captured_at": "2026-07-04T11:00:30+08:00",
                    "display_id": "main",
                    "app_name": "Dia",
                    "bundle_id": "company.thebrowser.dia",
                    "window_title": "ChatGPT project planning",
                    "url": "https://chatgpt.com/c/project",
                    "domain": "chatgpt.com",
                    "ocr_text": "project planning notes",
                    "screen_region_signal": {"region_kind": "main_content", "actionability": "actionable", "confidence": 0.9},
                },
                {
                    "captured_at": "2026-07-04T11:01:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "bundle_id": "md.obsidian",
                    "window_title": "Interview note",
                    "domain": "example.com",
                    "ocr_text": "local interview note",
                    "screen_region_signal": {"region_kind": "editor_body", "actionability": "actionable", "confidence": 0.8},
                },
                {
                    "captured_at": "2026-07-04T11:01:30+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "bundle_id": "md.obsidian",
                    "window_title": "Interview note",
                    "ocr_text": "writing local interview note",
                    "screen_region_signal": {"region_kind": "editor_body", "actionability": "actionable", "confidence": 0.8},
                },
            ],
        )

        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at="2026-07-04T11:02:00+08:00",
        )

        session = show_activity_session(self.conn, session_id)["session"]
        visible_apps = {item["label"] for item in session["visible_apps"]}
        primary_categories = {item["label"]: item for item in session["primary_focus_share"]}
        visible_categories = {item["label"]: item for item in session["visible_screen_share"]}
        top_domains = {item["label"] for item in session["top_domains"]}
        visible_domains = {item["label"] for item in session["visible_domains"]}

        self.assertIn("IINA", visible_apps)
        self.assertIn("工作 / AI 协作", primary_categories)
        self.assertNotIn("媒体娱乐", primary_categories)
        self.assertIn("媒体娱乐", visible_categories)
        self.assertGreater(visible_categories["媒体娱乐"]["duration_seconds"], 0)
        self.assertIn("chatgpt.com", top_domains)
        self.assertIn("chatgpt.com", visible_domains)
        self.assertNotIn("example.com", top_domains)
        self.assertNotIn("example.com", visible_domains)
        self.assertEqual(session["attribution_quality"]["status"], "ok")
        self.assertIn("visible_media_share", session["attribution_quality"])
        self.assertGreater(session["attribution_quality"]["visible_media_seconds"], 0)


    def test_activity_session_reports_attribution_quality_without_raw_details(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T11:30:00+08:00",
        )
        session_id = opened["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-04T11:30:00+08:00",
                    "display_id": "external",
                    "app_name": "Unknown",
                    "bundle_id": "",
                    "window_title": "local-private raw title",
                    "url": "https://example.com/private?q=token",
                    "domain": "example.com",
                    "ocr_text": "safe summary only",
                    "context_confidence": 0.32,
                    "context_source": "screencapturekit_keyframe+display_window_list_empty",
                    "display_attribution": {
                        "app_name": "Unknown",
                        "confidence": 0.32,
                        "source": "screencapturekit_keyframe+display_window_list_empty",
                        "reason_codes": ["display_window_list_empty"],
                    },
                    "screen_region_signal": {"region_kind": "unknown", "actionability": "unknown", "confidence": 0.2},
                }
            ],
        )

        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at="2026-07-04T11:31:00+08:00",
        )

        payload = show_activity_session(self.conn, session_id)
        session = payload["session"]
        quality = session["attribution_quality"]
        markdown = render_activity_sessions_markdown(payload)

        self.assertIn(quality["status"], {"watch", "poor"})
        self.assertGreater(quality["unknown_share"], 0)
        self.assertGreater(quality["low_confidence_share"], 0)
        self.assertTrue(quality["explanation_lines"])
        self.assertIn("attribution_reason_counts", quality)
        reasons = {item["reason"]: item for item in quality["attribution_reason_counts"]}
        self.assertIn("display_window_list_empty", reasons)
        self.assertIn("主要原因", " ".join(quality["explanation_lines"]))
        self.assertEqual(session["unknown_share"], quality["unknown_share"])
        self.assertIn("归因质量", markdown)
        self.assertIn("Unknown", markdown)
        self.assertIn("低置信", markdown)
        self.assertIn("窗口列表为空", markdown)
        self.assertNotIn("private?q=token", markdown)
        self.assertNotIn("/Users/", markdown)
        self.assertNotIn("ocr_text", markdown)


    def test_activity_sessions_status_summary_uses_open_session(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-04T12:00:00+08:00",
        )

        current = get_current_activity_session(self.conn)
        today = list_activity_sessions(
            self.conn,
            start="2026-07-04T00:00:00+08:00",
            end="2026-07-05T00:00:00+08:00",
        )

        self.assertEqual(current["status"], "ok")
        self.assertFalse(current["raw_access"])
        self.assertEqual(current["session"]["id"], opened["current_session"]["id"])
        self.assertIn("attribution_quality", current["session"])
        self.assertIn("unknown_share", current["session"])
        self.assertGreaterEqual(today["session_count"], 1)
        self.assertFalse(today["screen_time_boundary"]["system_screen_time_used"])


    def test_product_version_sources_are_in_sync(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        pyproject = tomllib.loads((repo_root / "core" / "pyproject.toml").read_text(encoding="utf-8"))
        browser_manifest = json.loads(
            (repo_root / "core" / "browser_extension" / "manifest.json").read_text(encoding="utf-8")
        )
        build_wrapper = (repo_root / "scripts" / "build-macos-app.sh").read_text(
            encoding="utf-8"
        )
        build_implementation = (
            repo_root / "scripts" / "build-macos-app-impl.sh"
        ).read_text(encoding="utf-8")

        self.assertEqual(CORE_VERSION, "0.9.1")
        self.assertEqual(pyproject["project"]["version"], CORE_VERSION)
        self.assertEqual(browser_manifest["version"], CORE_VERSION)
        self.assertEqual(AI_CONNECTOR_VERSION, CORE_VERSION)
        self.assertEqual(AI_HANDOFF_VERSION, CORE_VERSION)
        self.assertEqual(REPORT_CONTEXT_VERSION, CORE_VERSION)
        self.assertIn('MARKETING_VERSION="0.9.1"', build_implementation)
        self.assertNotIn("PAM_APP_VERSION", build_wrapper + build_implementation)
        self.assertIn('SWIFT_SDK_PATH="${PAM_TRUSTED_SWIFT_SDK:-}"', build_implementation)
        self.assertIn("PAM_TRUSTED_SWIFT_SDK", build_wrapper)
        self.assertNotIn(
            'SWIFT_SDK_PATH="${PAM_SWIFT_SDK',
            build_wrapper + build_implementation,
        )
        self.assertIn("PAM_SWIFT_SDK SDKROOT", build_implementation)
        self.assertIn("--disable-sandbox", build_implementation)
        self.assertIn("--manifest-cache none", build_implementation)
        self.assertIn("CLANG_MODULE_CACHE_PATH", build_implementation)
        self.assertIn('shasum -a 256 -c AGI-app-icon.sha256', build_implementation)
        self.assertIn('/bin/cp "$ROOT_DIR/assets/agi/AGI-app-icon.icns" "$ICNS_FILE"', build_implementation)
        self.assertNotIn('/usr/bin/iconutil', build_implementation)
        self.assertTrue((repo_root / "assets/agi/AGI-app-icon.icns").is_file())
        self.assertIn(
            "<key>CFBundleIconFile</key>\n  <string>AppIcon.icns</string>",
            build_implementation,
        )


    def test_menu_bar_label_does_not_embed_periodic_timeline(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        app_source = (
            repo_root
            / "apps"
            / "PersonalAIMonitorMenuBar"
            / "Sources"
            / "PersonalAIMonitorMenuBar"
            / "main.swift"
        ).read_text(encoding="utf-8")
        native_source = (repo_root / "apps/PersonalAIMonitorMenuBar/Sources/PersonalAIMonitorMenuBar/NativeStatusMenu.swift").read_text()
        self.assertNotIn("MenuBarExtra {", app_source)
        self.assertNotIn("TimelineView", native_source)
        self.assertIn("NSStatusBar.system.statusItem", native_source)
        self.assertIn("runner.statusSnapshot.menuBarTitle(now: now)", native_source)
        self.assertIn("NativeStatusMenu.shared.install()", app_source)


    def test_information_insufficient_cards_have_no_answer_ui(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        app_source = (
            repo_root
            / "apps"
            / "PersonalAIMonitorMenuBar"
            / "Sources"
            / "PersonalAIMonitorMenuBar"
            / "main.swift"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ActivityConfirmationCardInteractionPolicy.showsAnswerActions",
            app_source,
        )
        self.assertIn(
            "无需回答；系统只保留这条低打扰线索",
            app_source,
        )
        self.assertIn(
            "|| !ActivityConfirmationCardInteractionPolicy.showsAnswerActions(",
            app_source,
        )


    def test_release_privacy_scan_passes_clean_candidate_tree(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        inventory = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.split(b"\0")
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate_root = Path(temp_dir) / "candidate"
            for raw_relative in inventory:
                if not raw_relative:
                    continue
                relative = Path(os.fsdecode(raw_relative))
                source = repo_root / relative
                if not source.is_file():
                    continue
                destination = candidate_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
            scanner = candidate_root / "tools" / "release_privacy_scan.py"
            completed = subprocess.run(
                [sys.executable, str(scanner)],
                cwd=candidate_root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )



if __name__ == "__main__":
    unittest.main()
