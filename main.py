import asyncio
import re
from playwright.async_api import async_playwright, Playwright
import requests
import json
from datetime import datetime
from dotenv import load_dotenv
import os

# 加载 .env 文件
load_dotenv()  # 默认加载项目根目录的 .env 文件

has_sends_url = {} # 记录已发送通知的新闻链接,避免重复发送，最多存储1000条

dingding_token = os.getenv("dingding_token")
binance_accounts_str = os.getenv("binance_accounts", "")
binance_accounts = binance_accounts_str.split(",") if binance_accounts_str else []
effective_time = int(os.getenv("effective_time", "5"))
semaphore_limit = int(os.getenv("semaphore_limit", "1"))
keywords_str = os.getenv("keywords", "")
keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]

sem = asyncio.Semaphore(semaphore_limit)  # 最多 n 个并发

def parse_create_time(create_time: str):
    match = re.match(r'^(\d+)\s*(分钟|小时|天｜月｜年)(?:前)?$', create_time.strip())
    if not match:
        return 0, '未知'
    return int(match.group(1)), match.group(2)

async def binance_run(accounts):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        results = await asyncio.gather(
            *(visit_account(context, account) for account in accounts)
        )
        await browser.close()
    
async def visit_account(context: Playwright, account: str):
    async with sem:
        page = await context.new_page()
        url = f'https://www.binance.com/zh-CN/square/profile/{account}'
        print(f'Visiting URL: {url}')
        await page.goto(url)
        await page.wait_for_selector('.feed-layout-main', state='visible', timeout=20000)
        first_article_url = None
        index = 0
        while index < 5:  # 最多检查前5条，避免死循环
            card_locator = page.locator('.feed-layout-main .FeedList .feed-card').nth(index)
            try:
                await card_locator.wait_for(state='visible', timeout=5000)
                card_text = await card_locator.text_content() or ''
            except Exception as e:
                print(f'获取第 {index} 条卡片文本失败: {e}')
                index += 1
                continue
            if card_text != '' and '置顶' not in card_text:
                print(f'Found non-pinned article text: {card_text[:50]}..., trying to get URL')
                first_article_url = await card_locator.locator('.feed-content-text a').nth(0).get_attribute('href')
                break
            index += 1
        if not first_article_url:
            print('未找到文章，跳过')
            return
        detail_url = f'https://www.binance.com{first_article_url}'
        print(f'First article URL: {detail_url}, Index: {index}')
        await page.goto(detail_url)
        await page.wait_for_selector('.feed-layout-main', state='visible', timeout=20000)
        await asyncio.sleep(5)  # 等待页面完全加载，确保能获取到发布时间等信息
        create_time = await page.locator('.feed-layout-main .author .create-time').text_content()  # 14分钟 或 14 分钟前
        mins, ext = parse_create_time(create_time)
        print(f'Article create time: {create_time}, parsed as【{mins}】【{ext}】')
            
        if mins <= effective_time and ext == '分钟':
            # n 分钟前的新闻发送通知
            article_text = await page.locator('.feed-layout-main .richtext-container').text_content()
            if not check_keywords(article_text):
                print(f'文章内容不包含关键词，{article_text[:30]}...，跳过')
                return
            if has_sends_url.get(detail_url):
                print('该新闻已发送过通知，跳过')
                return        
            has_sends_url[detail_url] = True
            print('准备发送钉钉通知')
            res = send_dingtalk_markdown('bn报警通知: ' + account, article_text)
            print(res)
        else:
            print('新闻发布时间超过有效时间，跳过')
            
def check_keywords(article: str):
    """
    检查文章内容是否包含特定关键词
    :param article: 文章内容
    :return: bool, 是否包含关键词
    """
    return any(keyword in article for keyword in keywords)
    
def send_dingtalk_markdown(title, text, is_at_all=True):
    """
    发送钉钉 Markdown 格式消息
    
    :param title: 消息标题（显示在通知卡片上）
    :param text: Markdown 格式的消息内容
    :param is_at_all: 是否@所有人
    """
    headers = {'Content-Type': 'application/json'}
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": mark_down_template(title, text)
        },
        "at": {
            "atMobiles": [],
            "isAtAll": is_at_all
        }
    }
    webhook_url = f'https://oapi.dingtalk.com/robot/send?access_token={dingding_token}'
    response = requests.post(webhook_url, headers=headers, data=json.dumps(payload))
    return response.json()

def mark_down_template(title, text):
    return f"""
## {title}
#### {text}
#### 时间: {get_current_time()}
> author <sorry510sf@gmail.com>`
"""

def get_current_time():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

async def main():
    print('Starting Binance news monitoring...')
    while True:
        if has_sends_url.__len__() > 1000:
            has_sends_url.clear()  # 清理已发送记录，防止内存占用过高
        print('Checking Binance news...')
        await binance_run(binance_accounts)
        print('Waiting for 60 seconds before the next check...')
        # 每 60 秒检查一次
        await asyncio.sleep(60)
        
if __name__ == '__main__':      
    asyncio.run(main())