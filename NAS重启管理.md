# NAS 重启管理（1.8.0）

设置页提供重启下载器、重启 NAS，需输入“确认重启”。沿用保险箱老板账号鉴权与同源校验。NAS 重启中断全部应用；页面重连后恢复，暂停任务保持暂停。

后端保存 Aria2 会话并刷新数据库后，向仅用于固定管理动作的挂载目录提交请求。主机定时服务再次保存会话、消费请求后执行固定 systemctl 命令；容器无 Docker socket、无特权模式、无任意命令接口。保存失败时取消请求。主机结果在 admin-control/last-result.json，故障详情可查看 nova-vault-admin.service 日志。

下载器以原有 /root/.aria2/aria2.conf 与 session 启动。保留已有 continue=true、input-file、save-session、每 60 秒保存和 force-save 配置，改用 systemd 正常 SIGTERM 停止（最长 120 秒），异常退出自动启动。Docker、下载器和管理定时服务启用开机启动；应用与 HTTPS 网关容器 restart policy 为 unless-stopped。

验收：84 项测试通过，真实固定动作桥重启下载器成功，前后均有 20 个活跃、16 个等待任务，恢复后有下载速度；未登录重启接口 401。公网 healthz 返回 1.8.0。整机未实际重启，因此断电及整机启动后的完整恢复未做实机验收。

回滚应用可使用 nova-download-vault-before-1.8.0；原 Aria2 启动脚本仍保留 /etc/init.d/aria2，配置备份 /root/.aria2/aria2.conf.before-vault180。回滚下载器前必须保存会话并正常终止进程，不使用旧脚本的 kill -9。
