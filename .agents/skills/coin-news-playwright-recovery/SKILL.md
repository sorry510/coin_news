---
name: coin-news-playwright-recovery
description: Diagnose and repair intermittent Binance Square Playwright feed timeouts in the coin_news monitor. Use when visit_account or detail-page navigation times out waiting for .feed-layout-main.
version: 1.0.0
triggers:
  - coin_news feed-layout-main timeout
  - Binance Square Playwright timeout
  - visit_account TimeoutError
---
# Coin News Playwright Recovery

Use this workflow only in the `coin_news` repository.

1. Confirm the worktree state and preserve unrelated changes. Disable `dingding_token` for every diagnostic run so read-only debugging cannot send alerts.
2. Read `visit_account`, the profile/detail selectors, and recent selector history. Do not call a selector obsolete based only on one timeout.
3. Run one real browser cycle with the configured public account. If Chromium is blocked by the macOS sandbox, report that environment failure separately and request the minimum escalation needed for browser launch. Stop the process after it reaches the 60-second sleep.
4. Compare the evidence: a successful current run proves `.feed-layout-main` still exists; an intermittent failure should be handled as navigation, WAF, or resource-load instability unless a captured page proves DOM drift.
5. Centralize profile and detail navigation in one helper. Use bounded retries, keep the existing selector when verified, and include HTTP status, final URL, title, a short body excerpt, and the underlying Playwright error in the final failure.
6. If `.feed-layout-main` loads but `.richtext-container` times out, inspect a fresh real-page DOM before changing selectors. Prefer a verified semantic fallback such as `.article-body`; collect the profile-card preview before navigating away; then fall back to the longest available profile preview or `description`/`og:description` metadata. If every source is empty, skip only that post instead of failing the account cycle.
7. Verify primary extraction and forced-missing-selector fallback separately, then run `python -m py_compile`, `git diff --check`, and one real cycle with DingTalk disabled. Clearly distinguish static checks, mocked branch checks, and real-browser validation.
8. Re-check the diff and preserve concurrent user edits such as environment-default changes.