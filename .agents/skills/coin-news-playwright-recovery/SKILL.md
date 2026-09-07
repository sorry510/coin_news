---
name: coin-news-playwright-recovery
description: Diagnose and repair intermittent Binance Square Playwright feed timeouts in the coin_news monitor. Use when visit_account or detail-page navigation times out waiting for .feed-layout-main.
metadata:
  version: 1.0.3
  trusted: false
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

9. For HTTP 202, inspect `x-amzn-waf-action` and the main-document response chain. A challenge response can run site-provided JavaScript, obtain a token, and automatically re-request the same URL. Keep that page alive and allow bounded validation time; do not immediately reject 202 or repeatedly interrupt the script with navigation. Verify recovery using the final document status and a loaded feed, not the initial `page.goto` status. Headerless 202 can still render normally. See the [AWS action behavior](https://docs.aws.amazon.com/waf/latest/developerguide/waf-captcha-and-challenge-actions.html).
10. When the user needs normal access restored, trace the browser lifecycle before adding skip/fallback branches. Reuse the browser context across polling rounds; compare original and native Chromium settings with live evidence instead of assuming a fingerprint change fixes WAF. The monitor uses full Chromium (`channel='chromium'`) with a dedicated `.browser-data/` directory, which must stay Git-ignored and cannot be shared by simultaneous processes. Serialize navigation starts while preserving configured account processing concurrency.
11. Treat unresolved `WafChallengeError` as a session failure: block queued navigation, cancel the remaining account tasks, and let the monitor retain the session for a bounded cooldown and retry. Do not use profile previews or single-post skipping as the WAF fix. Ordinary rendering failures after successful access may still use the verified article extraction fallbacks. A later network failure must not erase an unresolved access restriction.
12. Verify `python -m unittest -q test_main` with DingTalk disabled, then run two real rounds in one isolated browser session. Check the originally failing link, challenge-to-200 recovery, context reuse, task cancellation, and per-line timestamp formatting including multiline exceptions. Report observed successes and remaining deployment limitations; a completed JavaScript challenge is not evidence that future requests can never be challenged.
