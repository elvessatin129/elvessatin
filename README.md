# TeamViewer Contact Automation

该脚本使用 [Playwright](https://playwright.dev/python/) 模拟浏览器操作，帮助你在 TeamViewer 网页端（https://account.teamviewer.com/ 和 https://web.teamviewer.com/contacts）批量添加联系人：

1. 从本地 TXT 文件读取邮箱列表（可选带名称）。
2. 自动登录网页端并打开联系人页。
3. 逐个添加联系人，成功后把“名称 + 邮箱”写入 `data/successful_contacts.csv`。
4. 失败的项目可以写入 `data/failed_contacts.csv` 方便排查。

> 说明：由于 TeamViewer 网页 UI 可能随版本变动，脚本依赖的 CSS/可访问性选择器可能需要你根据实际页面调整。仓库提供 `config/selectors.example.json` 作为示例模板。

## 环境准备

- Python 3.10+
- 可访问外网的环境（登录 TeamViewer 与下载 Playwright 浏览器内核）。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 输入文件格式

把需要添加的邮箱写入文本文件（默认读取 `data/sample_contacts.txt`）：

```
# 支持注释行
姓名A,email_a@example.com
email_b@example.com
```

- 如果一行包含英文逗号，左侧视为联系人名称，右侧视为邮箱。
- 只有邮箱时，脚本会自动用邮箱前缀推导一个名称（`john.doe` → `John Doe`）。

## 选择器配置（可选）

1. 复制模板：`cp config/selectors.example.json config/selectors.json`
2. 根据浏览器开发者工具实际看到的元素属性，修改里面的选择器。例如把 `button[data-testid='add-contact-button']` 换成你页面真实的 `data-testid` 或可访问名称。
3. 运行脚本时通过 `--selectors config/selectors.json` 指定；如果不指定则使用内置默认值。

支持的选择器类型：

- `css`: 普通 CSS 选择器。
- `role`: 使用可访问角色与名称（Playwright `get_by_role`）。
- `label`: 根据 `<label>` 绑定文本。
- `placeholder`: 根据输入框 placeholder。
- `text`: 根据可见文字内容。
- `test_id`: Playwright `get_by_test_id`。

每个步骤会按数组顺序依次尝试，直到某个选择器找到元素。

## 运行示例

```bash
python scripts/teamviewer_contact_adder.py \
  --username your_account@example.com \
  --email-file data/sample_contacts.txt \
  --selectors config/selectors.json \
  --success-log data/successful_contacts.csv \
  --failure-log data/failed_contacts.csv
```

运行时如未传 `--password`，程序会安全地提示输入；也可预先设置环境变量 `TEAMVIEWER_PASSWORD`。

常用可选参数：

- `--headless`：无界面模式运行。
- `--slowmo 250`：为每一步增加延时，方便观察。
- `--post-login-wait`：登录后进入联系人页前的等待秒数。
- `--action-timeout`：等待弹窗、提示信息的超时时间（秒）。

## 输出文件

- `data/successful_contacts.csv`：成功添加的联系人（只包含名称与邮箱）。
- `data/failed_contacts.csv`：失败记录（邮箱、可能的名称、错误原因）。

> 根据你的需求，第 2、3 点——只记录添加成功且确实存在的邮箱——已经内置在逻辑里：只有检测到成功提示时才会写入成功表；未检测到成功提示就视为失败，不会保存。

## 排查建议

- 运行前用浏览器手动登录一次，确认不会触发验证码或多因素验证。
- 若脚本提示找不到元素，使用 DevTools 检查最新的 DOM，并更新选择器。
- 可在非 headless 模式下配合 `--slowmo` 观察自动化流程。
- 如果 TeamViewer 账户开启了 MFA，需要你手动在脚本暂停时完成验证后再继续。

## 安全提示

- 不要把真实密码写进脚本或仓库；建议使用环境变量或运行时输入。
- 运行自动化前确认遵守 TeamViewer 使用条款，避免过快的批量操作造成封锁。
