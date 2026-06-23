# Grok 账号批量注册工具

基于 [DrissionPage](https://github.com/g1879/DrissionPage) 的 Grok (x.ai) 账号自动注册脚本，使用 [Smailpro](https://smailpro.com) 临时邮箱接收验证码，通过 Chrome 扩展修复 CDP `MouseEvent.screenX/screenY` 缺陷绕过 Cloudflare Turnstile。

注册完成后自动推送 SSO token 到 [grok2api](https://github.com/chenyme/grok2api) 号池。

## 特性

- Smailpro 临时邮箱（`curl_cffi` TLS 指纹伪装）
- Cloudflare Turnstile 自动绕过（Chrome 扩展 patch `MouseEvent.screenX/screenY`）
- 域名拒绝检测（邮箱域名被 x.ai 拒绝时自动记录并跳过）
- 无头服务器支持（Xvfb 虚拟显示器，自动检测 Linux 环境）
- 中英文界面自动适配
- 自动推送 SSO token 到 grok2api（支持 append 合并模式）
- 邮箱尝试日志（`logs/email_attempts.log`，JSON Lines 格式）

---

## 环境要求

- Python 3.10+
- Chromium 或 Chrome 浏览器
- [grok2api](https://github.com/chenyme/grok2api) Docker 实例（用于提供代理清理 CF 流量 + 自动导入 SSO token）

---

## 安装

```bash
pip install -r requirements.txt
```

无头服务器（Linux）额外安装：

```bash
apt install -y xvfb
pip install PyVirtualDisplay
# 推荐用 playwright 装 chromium（避免 snap 版 AppArmor 限制）
pip install playwright && python -m playwright install chromium && python -m playwright install-deps chromium
```

---

## 部署 grok2api（Docker）

本脚本需要配合 Docker 中运行的 [grok2api](https://github.com/chenyme/grok2api) 使用。grok2api 提供：

1. **代理服务** — 通过 CF 清理流量，浏览器通过该代理访问 x.ai，避免直连被风控
2. **SSO token 管理接口** — 注册完成后自动推送 token 到号池

请参考 [grok2api 文档](https://github.com/chenyme/grok2api) 部署 Docker 实例，部署完成后记录其管理接口地址和 `app_key`。

---

## 配置文件（config.json）

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
    "run": { "count": 10 },
    "proxy": "",
    "browser_proxy": "",
    "api": {
        "endpoint": "",
        "token": "",
        "append": true
    }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `run.count` | int | 注册轮数，`0` 为无限循环，可通过 `--count` 覆盖 |
| `proxy` | string | Smailpro API 请求代理（可选） |
| `browser_proxy` | string | 浏览器代理，指向 grok2api Docker 提供的代理地址，用于通过 CF 清理流量访问 x.ai |
| `api.endpoint` | string | grok2api 管理接口地址，留空跳过推送 |
| `api.token` | string | grok2api 的 `app_key` |
| `api.append` | bool | `true` 合并线上已有 token，`false` 覆盖 |

---

## 启动方式

```bash
# 按 config.json 中 run.count 执行（默认 10 轮）
python DrissionPage_example.py

# 指定轮数
python DrissionPage_example.py --count 50

# 无限循环
python DrissionPage_example.py --count 0


# linux上无头启动（需要使用这种方式才能够运行）
xvfb-run --server-args="-screen 0 1024x768x24" .venv/bin/python DrissionPage_example.py --count 3000
```

无头服务器会自动启用 Xvfb，无需额外配置。

---

## 输出文件

```
sso/
  sso_<timestamp>.txt     ← 每行一个 SSO token
logs/
  run_<timestamp>.log     ← 每轮注册的邮箱、密码和结果
  email_attempts.log      ← 每次邮箱尝试的详细记录（JSON Lines）
```

目录在首次运行时自动创建。

---

## 文件结构

```
├── DrissionPage_example.py     # 主脚本
├── email_register.py           # Smailpro 临时邮箱封装
├── config.json                 # 配置文件（不入库）
├── config.example.json         # 配置模板
├── requirements.txt            # Python 依赖
├── turnstilePatch/             # Chrome 扩展（Turnstile patch）
│   ├── manifest.json
│   └── script.js
├── sso/                        # SSO token 输出（自动创建）
└── logs/                       # 运行日志（自动创建）
```

---

## 无头服务器部署注意

- snap 版 chromium 在 root 下有 AppArmor 限制，推荐用 playwright 安装的 chromium
- 服务器需通过 grok2api Docker 代理访问 x.ai，在 `browser_proxy` 填写代理地址
- 脚本自动检测 Linux 环境并启用 Xvfb + playwright chromium 路径

---

## 致谢

- [kevinr229/grok-maintainer](https://github.com/kevinr229/grok-maintainer) — 原始项目
- [grok2api](https://github.com/chenyme/grok2api) — Grok API 代理
- [Smailpro](https://smailpro.com) — 临时邮箱服务
