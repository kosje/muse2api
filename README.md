# Muse2API — kosje 安全分支

基于 `yys9253462-gif/muse2api` 的 `15944062b430d009cf3b88a920bf1069dc957a9f`。
上游项目为 [czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api)，保留 MIT 许可证。

本分支用于个人部署。账号登录与视频生成仍由 muse.ai 提供；请只导入自己的账号。
默认仅允许本机连接，推荐通过 SSH 隧道访问。多用户隔离与计费不在本版本范围。

## 部署

优先使用 [配套安全安装器](https://github.com/kosje/muse-video-installer/tree/security/private-deployment)，
其中 `release.json` 固定本仓库的经过验证的 commit。
需要 Docker Engine 28+ / Compose v2 和 Linux。当前容器 CI 为 linux/amd64。

也可在已检出的审查版本运行 `docker compose up -d --build`。
网页 `http://127.0.0.1:18610/`，管理 `http://127.0.0.1:18610/admin`。
远程访问：`ssh -N -L 18610:127.0.0.1:18610 用户@服务器`。

首次启动在持久化卷 `/app/data/auth.json` 创建两把不同的随机密钥，不写入日志。
本机管理员可运行以下命令查看（只在自己的终端运行，勿公开输出）：

```sh
docker compose exec api python -c 'import json; print(json.load(open("/app/data/auth.json")))'
```

- `admin`：登录管理页面、导入/删除账号、查看和轮换生成接口 Key。
- `api`：视频工作台及客户端生成请求，可访问同一实例的生成文件。
- 使用 `Authorization: Bearer ...`，不接受 URL 查询参数里的 Key。
- Cookie 工具从管理页面下载，使用管理员 Key。只接受 HTTPS 或 `127.0.0.1` / `::1` 的 HTTP SSH 隧道。
- 浏览器扩展不再分发；避免安装旧版本的广泛权限扩展。

## 安全边界

- 只提供 `/`、`/admin` 两个明确的 HTML 文件；无目录静态挂载。
- 非 root 容器、只读根文件系统、无额外 capabilities、无 Docker socket、CDP 仅容器内本机可见。
- 删除 GitHub 在线拉取/推送和自动更新代码；应用无法写入自身源代码。
- 管理 API 只接受管理员 Key。缺失或损坏的密钥文件不会关闭鉴权。
- 密钥轮换原子写入数据卷；重启/重建不恢复旧 Key。初始环境变量只对全新数据卷生效。
- 媒体必须带 Bearer Key；浏览器用一小时有效、HttpOnly、SameSite=Strict、仅 `/v1/media/` 有效的签名 Cookie，密钥轮换会使对应 Cookie 失效。
- 媒体 URL 不再是公开分享链接；无法发送 Authorization 的外部客户端需自行下载后渲染。
- 参考图片仅下载到固定解析得到的公网 IP，HTTPS 保留证书验证和 SNI。不跟随重定向，限制 20 MiB、响应类型和超时；不读取本地文件。
- 前端同源访问，默认不启用 CORS；凭据只留在 sessionStorage。
- Python 依赖由 `requirements.lock` 固定版本与哈希，基础镜像固定 digest。

Chromium 为兼容容器仍使用 `--no-sandbox`；隔离依赖容器边界，请部署在专用虚拟机，
不要把宿主凭据或其他业务数据挂载进容器。Debian/Chromium 软件包使用官方签名源，
会随主动重建获得更新，未声称整个镜像可按字节重现。固定版本仍需定期审查安全更新。
数据卷中的 Cookie 是明文凭据；离线备份、服务器和磁盘访问控制同样需要保护。

## 验证

```sh
pip install --require-hashes -r requirements.lock
pip install pytest==9.1.1 httpx==0.28.1
python -m pytest tests/test_security.py -q
python tests/test_async_images.py
python tests/test_vm_wait.py engine.py --assert
```

GitHub Actions 额外构建容器、验证非 root/只读部署、匿名访问边界，并运行离线 Chromium 媒体选择回归。
这些测试不登录 muse.ai，不能替代部署后由账号所有者完成的一次真实生成验证。

## 旧版迁移

先关闭原网页服务及公网入口；旧版曾暴露公网的 API Key/Cookie 应视作可能泄露。
在独立目录新装本版、重新登录并导入账号，不复用旧密钥。
确需保留生成文件时，只在停写后复制 `data/media/` 中的普通媒体文件，勿复制符号链接、`.env`、`auth.json` 或整个旧目录。
完成新站验证前保留旧数据的离线副本。不要继续运行旧版 `python -m http.server --directory /opt/mvw`。
