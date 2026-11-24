# PSI 本地网页工作台

[English](README.md)

PSI 把待决定的问题和已有处理记录放在一个网页里。点开一条事项，就能查看依据、建议的下一步，也可以写下自己的回复草稿。公开版自带虚构示例，一台电脑上的浏览器和 Python 就能运行。

## 启动

在装有 Python 3.12 或更高版本的 macOS 或 Linux 电脑上，打开终端运行：

```sh
git clone https://github.com/Samuel-Shek/AGI.git
cd AGI
python3 -B web/psi_sync_center.py
```

用同一台电脑的浏览器打开 **[http://127.0.0.1:8767/](http://127.0.0.1:8767/)**。选一条问题可以查看依据、试写草稿。默认使用本次会话的临时目录，在终端按 Ctrl+C 即可退出。

想保留草稿或更换端口，可查看[启动说明](docs/INSTALL.md)。

## 数据与开源范围

默认启动只读取随源码提供的示例。个人活动、对话、本地智能助手和后台服务都无需接入。仓库也保留了底层 Python 代码，方便审阅来源授权、隐私过滤和审计逻辑。已有独立授权的本机后台可显式接入。[公开源码范围](docs/PUBLIC_SOURCE_SCOPE.md) · [隐私说明](docs/PRIVACY.md)

自动测试覆盖网页工作台和部分底层功能。自动修复和反思提醒在日常使用中能带来多少收益，仍需实际使用记录验证。[贡献指南](CONTRIBUTING.md) · [安全问题反馈](SECURITY.md)

本项目按 [AGPL-3.0-only](LICENSE) 开源。第三方组件的许可见 [NOTICE](NOTICE) 和[来源记录](core/evolution/ai_ceo/vendor/PROVENANCE.json)。
