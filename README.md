## 本地部署
### 安装依赖
```commandline
pip install -r requirements.txt
```

### 安装chromium插件
```commandline
playwright install chromium
```

监控使用完整 Chromium 的无头模式，需要执行上述安装命令；仅安装 `--only-shell` 不够。

## 运行
```commandline
python main.py
```

## uv

### 安装
```
uv venv 
uv pip install -r requirements.txt
```

### 运行

```
uv run main.py
```

## 浏览器会话与日志

浏览器在各轮检查之间保持运行，使用项目目录下的 `.browser-data/` 保存站点存储和缓存。该目录已加入 Git 忽略；同一目录只能由一个监控进程使用。

遇到 `HTTP 202 / WAF challenge` 时，保留当前页面，让网站自带的 JavaScript 验证完成并自动重新请求正文。同一会话的页面导航串行执行，开始时间至少间隔 3 秒。验证持续失败或收到访问限制时，整轮监控暂停并告警，保留会话等待 60 秒后重新检查，不用摘要冒充详情页读取成功。

所有程序日志统一输出到标准输出，每行包含运行机器的当前本地时间、时区偏移和日志级别，多行异常也逐行加前缀：

```text
[2026-09-06 20:31:17 +0800] [INFO] 浏览器验证完成，正文已加载: HTTP=200, URL=...
```

更新后重启监控进程；使用仓库 PM2 配置部署时可执行 `pm2 restart coin_news`。

## 服务器出现 Human Verification / HTTP 405

`WAF=captcha` 需要人工完成验证码，与可自动完成的 `WAF=challenge` 分开处理。程序会立即暂停整轮检查，保留当前浏览器页面，不再每 45 秒超时重试。通过验证后自动恢复监控，仍使用 `python main.py` 启动。

只有 SSH 的服务器也可操作：遇到 CAPTCHA 时，程序会临时在 `127.0.0.1:8765` 启动验证入口，并在日志中显示带随机访问令牌的链接。在自己电脑上建立 SSH 转发：

```commandline
ssh -N -L 8765:127.0.0.1:8765 <SSH用户>@<服务器地址>
```

保持该终端运行，在本机浏览器打开日志中的 `http://127.0.0.1:8765/随机令牌/`。画面来自服务器当前浏览器，点击图片操作验证码，需要输入文字时使用下方输入框。验证通过且正文出现后，入口自动关闭，监控继续。不需要安装桌面、VNC 或新增 Python 依赖，也不需要开放服务器公网端口。

不要同时另起一个监控进程；当前进程正在保留验证会话。日志中的入口仅在本次验证期间有效。若有服务器桌面，也可使用 `python main.py --headed` 显示窗口操作。

这条日志不能确定币安内部具体命中了哪条风控规则；程序不会自动作答验证码，也不能保证今后不再要求验证。

## 回归测试

```commandline
dingding_token= python -m unittest -q test_main test_manual_verification
```

测试覆盖验证完成后的正文读取、持续风控的整轮处理、会话复用、导航间隔和多行日志时间格式，不发送钉钉消息。
