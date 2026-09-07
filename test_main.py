import asyncio
import logging
import re
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main


def response(status=200, waf=''):
    return SimpleNamespace(status=status, headers={'x-amzn-waf-action': waf} if waf else {})


def mock_page(responses):
    page = MagicMock()
    page.url = 'https://www.binance.com/zh-CN/square/post/test'
    page.goto = AsyncMock(side_effect=responses)
    page.wait_for_selector = AsyncMock()
    page.title = AsyncMock(return_value='')
    page.locator.return_value.inner_text = AsyncMock(return_value='')
    return page


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_captcha_never_retries_or_waits_for_feed(self):
        page = mock_page([response(405, 'captcha')])
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(main.HumanVerificationRequired):
                await main.goto_feed_page(page, page.url)
        page.goto.assert_awaited_once()
        page.wait_for_selector.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_challenge_upgrading_to_captcha_interrupts_feed_wait(self):
        page = mock_page([response(202, 'challenge')])
        stopped = asyncio.Event()

        async def upgrade(*args, **kwargs):
            final = response(405, 'captcha')
            final.request = SimpleNamespace(is_navigation_request=lambda: True, frame=page.main_frame)
            page.on.call_args.args[1](final)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        page.wait_for_selector.side_effect = upgrade
        with self.assertRaises(main.HumanVerificationRequired):
            await asyncio.wait_for(main.goto_feed_page(page, page.url), timeout=1)
        page.goto.assert_awaited_once()
        self.assertTrue(stopped.is_set())

    async def test_challenge_finishes_in_place_without_a_second_goto(self):
        page = mock_page([response(202, 'challenge')])

        async def finish(*args, **kwargs):
            callback = page.on.call_args.args[1]
            final = response()
            final.request = SimpleNamespace(is_navigation_request=lambda: True, frame=page.main_frame)
            callback(final)

        page.wait_for_selector.side_effect = finish
        with self.assertLogs(main.logger, level='INFO') as logs:
            await main.goto_feed_page(page, page.url)
        page.goto.assert_awaited_once()
        self.assertEqual(page.wait_for_selector.call_args.kwargs['timeout'], main.CHALLENGE_TIMEOUT)
        self.assertTrue(any('HTTP=200' in log for log in logs.output))
        page.remove_listener.assert_called_once()

    async def test_persistent_challenge_is_a_session_failure(self):
        page = mock_page([response(202, 'challenge')] * 3)
        page.wait_for_selector.side_effect = main.PlaywrightError('challenge never completed')
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(main.WafChallengeError, 'HTTP=202, WAF=challenge'):
                await main.goto_feed_page(page, page.url)
        self.assertEqual(page.goto.await_count, 3)
        self.assertEqual(page.remove_listener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [10, 20])

    async def test_headerless_202_can_render(self):
        page = mock_page([response(202)])
        await main.goto_feed_page(page, page.url)
        page.wait_for_selector.assert_awaited_once()

    async def test_dom_timeout_on_200_is_not_a_waf_failure(self):
        page = mock_page([response()] * 3)
        page.wait_for_selector.side_effect = main.PlaywrightError('missing selector')
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            with self.assertRaises(main.FeedPageLoadError) as caught:
                await main.goto_feed_page(page, page.url)
        self.assertNotIsInstance(caught.exception, main.WafChallengeError)

    async def test_http_429_stops_the_session(self):
        page = mock_page([response(429)] * 3)
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            with self.assertRaises(main.WafChallengeError):
                await main.goto_feed_page(page, page.url)
        page.wait_for_selector.assert_not_awaited()

    async def test_network_failure_does_not_erase_unresolved_waf(self):
        page = mock_page([response(202, 'challenge'), main.PlaywrightError('network'), main.PlaywrightError('network')])
        page.wait_for_selector.side_effect = main.PlaywrightError('challenge never completed')
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            with self.assertRaises(main.WafChallengeError):
                await main.goto_feed_page(page, page.url)

    async def test_missing_time_has_bounded_reads(self):
        page = MagicMock()
        page.locator.return_value.text_content = AsyncMock(side_effect=main.PlaywrightError('missing'))
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            self.assertEqual(await main.get_create_time(page), '')
        self.assertEqual(page.locator.return_value.text_content.await_count, 7)
        for call in page.locator.return_value.text_content.await_args_list:
            self.assertEqual(call.kwargs['timeout'], 1000)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_captcha_pauses_until_human_completion_then_resumes(self):
        page = MagicMock()
        page.close = AsyncMock()
        context = MagicMock()
        entered = asyncio.Event()
        completed = asyncio.Event()

        async def human(_page):
            entered.set()
            await completed.wait()

        with (
            patch.object(main, 'binance_run', new_callable=AsyncMock,
                         side_effect=[main.HumanVerificationRequired(page), asyncio.CancelledError]) as run,
            patch.object(main, 'wait_for_human_verification', side_effect=human),
            patch.object(main, 'notify_error'),
        ):
            task = asyncio.create_task(main.monitor_loop(context, ['account']))
            await entered.wait()
            self.assertEqual(run.await_count, 1)
            page.close.assert_not_awaited()
            completed.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(run.await_count, 2)
        page.close.assert_awaited_once()

    async def test_gate_blocks_other_navigation_after_waf_failure(self):
        gate = main.NavigationGate()
        with self.assertRaises(main.WafChallengeError):
            async with gate:
                raise main.WafChallengeError('verification failed')
        page = mock_page([response()])
        with self.assertRaises(main.WafChallengeError):
            await main.goto_feed_page(page, page.url, gate)
        page.goto.assert_not_awaited()
        self.assertFalse(gate.lock.locked())

    async def test_gate_spaces_navigation_starts(self):
        gate = main.NavigationGate()
        with patch.object(main.time, 'monotonic', return_value=100), patch.object(main.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            async with gate:
                pass
            async with gate:
                pass
        sleep.assert_awaited_once_with(main.NAVIGATION_INTERVAL)

    async def test_waf_failure_cancels_remaining_accounts(self):
        cancelled = asyncio.Event()
        started = asyncio.Event()

        async def visit(context, account, gate):
            if account == 'failed':
                await started.wait()
                raise main.WafChallengeError('challenge')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(main, 'visit_account', side_effect=visit):
            with self.assertRaises(main.WafChallengeError):
                await main.binance_run(['failed', 'pending'], MagicMock())
        self.assertTrue(cancelled.is_set())

    async def test_main_reuses_same_context_across_rounds(self):
        context = MagicMock()
        entries = []

        @asynccontextmanager
        async def session(**options):
            entries.append(context)
            yield context

        with (
            patch.object(main, 'browser_session', session),
            patch.object(main, 'binance_run', new_callable=AsyncMock) as run,
            patch.object(main.asyncio, 'sleep', new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError]),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.main()
        self.assertEqual(entries, [context])
        self.assertEqual(run.await_count, 2)
        self.assertTrue(all(call.args[1] is context for call in run.await_args_list))


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_captcha_page_is_preserved(self):
        page = MagicMock()
        page.close = AsyncMock()
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with (
            patch.object(main, 'sem', asyncio.Semaphore(1)),
            patch.object(main, 'goto_feed_page', new_callable=AsyncMock,
                         side_effect=main.HumanVerificationRequired(page)),
        ):
            with self.assertRaises(main.HumanVerificationRequired):
                await main.visit_account(context, 'account')
        page.close.assert_not_awaited()

    async def test_waf_failure_never_uses_preview_or_skips_to_next_post(self):
        card = MagicMock()
        card.wait_for = AsyncMock()
        card.text_content = AsyncMock(return_value='上线公告')
        content = card.locator.return_value.nth.return_value
        content.inner_text = AsyncMock(return_value='上线公告')
        content.locator.return_value.nth.return_value.get_attribute = AsyncMock(return_value='/zh-CN/square/post/1')
        page = MagicMock()
        page.locator.return_value.nth.return_value = card
        page.close = AsyncMock()
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with (
            patch.object(main, 'sem', asyncio.Semaphore(1)),
            patch.object(main, 'goto_feed_page', new_callable=AsyncMock, side_effect=[None, main.WafChallengeError('challenge')]) as goto,
            patch.object(main, 'get_article_text', new_callable=AsyncMock) as extract,
            patch.object(main, 'send_dingtalk_markdown') as send,
            patch.object(main, 'notify_error') as notify,
        ):
            with self.assertRaises(main.WafChallengeError):
                await main.visit_account(context, 'account')
        self.assertEqual(goto.await_count, 2)
        extract.assert_not_awaited()
        send.assert_not_called()
        notify.assert_not_called()
        page.close.assert_awaited_once()


class LoggingTests(unittest.TestCase):
    def test_every_physical_line_has_same_timestamp_and_level(self):
        formatter = main.TimestampFormatter('%(message)s', datefmt='%Y-%m-%d %H:%M:%S %z')
        record = logging.LogRecord('test', logging.WARNING, '', 0, 'timeout\nCall log:\n\n- missing selector', (), None)
        lines = formatter.format(record).splitlines()
        self.assertEqual(len(lines), 4)
        prefix = re.match(r'^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}\] \[WARNING\] ', lines[0]).group()
        self.assertTrue(all(line.startswith(prefix) for line in lines))

    def test_logging_formats_payloads_and_arguments(self):
        formatter = main.TimestampFormatter('%(message)s', datefmt='%Y-%m-%d %H:%M:%S %z')
        record = logging.LogRecord('test', logging.INFO, '', 0, '结果: %s', ({'ok': True},), None)
        self.assertIn("结果: {'ok': True}", formatter.format(record))

    def test_empty_message_still_has_a_timestamp(self):
        formatter = main.TimestampFormatter('%(message)s', datefmt='%Y-%m-%d %H:%M:%S %z')
        record = logging.LogRecord('test', logging.INFO, '', 0, '', (), None)
        self.assertRegex(formatter.format(record), r'^\[\d{4}-\d\d-\d\d .*\] \[INFO\] $')


if __name__ == '__main__':
    unittest.main()
