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
        while not first_article_url:
            cardText = await page.locator('.feed-layout-main .FeedList .feed-card').nth(index).all_text_contents()
            if '置顶' not in cardText:
                first_article_url = await page.locator('.feed-layout-main .FeedList .feed-content-text a').nth(index).get_attribute('href') # 找到第一个非置顶，进入明细页面
            index += 1
        detail_url = f'https://www.binance.com{first_article_url}'
        print(f'First article URL: {detail_url}')
        await page.goto(detail_url)
        await page.wait_for_selector('.feed-layout-main', state='visible', timeout=20000)
        await asyncio.sleep(3)  # 等待页面完全加载，确保能获取到发布时间等信息
        create_time = await page.locator('.feed-layout-main .author .create-time').text_content()  # 14分钟 或 14 分钟前
        mins, ext = parse_create_time(create_time)
        print(f'Article create time: {create_time}, parsed as {mins} {ext}')
            
        if mins <= effective_time and ext == '分钟':
            # 3 分钟前的新闻发送通知
            article_text = await page.locator('.feed-layout-main .richtext-container').text_content()
            if not check_keywords(article_text):
                return
            if has_sends_url.get(detail_url):
                print('该新闻已发送过通知，跳过')
                return        
            has_sends_url[detail_url] = True
            print('准备发送钉钉通知')
            res = send_dingtalk_markdown('bn报警通知: ' + account, article_text)
            print(res)
            
def check_keywords(article: str):
    """
    检查文章内容是否包含特定关键词
    :param article: 文章内容
    :return: bool, 是否包含关键词
    """
    keywords = ['活动', '奖励', 'Alpha', '上线', '上市', 'alpha', '空投', '赠送', '福利', '送出', '补贴', '交易大赛', '交易竞赛', '抽取', '抽奖', '赠币', '奖励金', '奖励计划']
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