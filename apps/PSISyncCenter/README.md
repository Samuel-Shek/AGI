# PSI 事项工作台

工作台可在 PSI 桌面应用里打开，也能作为只在本机运行的网页使用。「问题」列出需要本人决定的事项，「处理记录」显示进展和历史依据。保存笔记、交给 Codex 和确认完成分别记录。

## 用虚构资料试用

在仓库根目录运行下面的命令。终端会显示本地地址，按 Ctrl+C 结束演示。草稿保存在本次创建的临时目录。

```bash
python3 -B apps/PSISyncCenter/psi_sync_center.py --fixture apps/PSISyncCenter/tests/fixtures/demo.json --data-dir "$(mktemp -d)" --standalone --port 8767
```

网页只接受本机回环访问。真实事项使用当前账号已授权的 PSI 后台。接手一项工作时，工作台会核对事项的当前版本，并用原有编号查回执。运行真实资料模式需要本机安装配套后台和相应的接口。桌面内嵌工作台和真实后台连接使用 python.org 的 Python 3.12 Framework，解释器路径为 `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`。演示模式只读取随附的虚构资料。

## 开发验证

```bash
python3 -B -m unittest discover -s apps/PSISyncCenter/tests
node apps/PSISyncCenter/tests/frontend_check.js apps/PSISyncCenter/static
```
