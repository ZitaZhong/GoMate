# 「周末去哪儿」Linux 服务器部署手册

本文档介绍如何在 Linux 服务器上部署「周末去哪儿」项目。

---

## 目录

- [系统要求](#系统要求)
- [快速部署](#快速部署)
- [详细步骤](#详细步骤)
- [配置说明](#配置说明)
- [验证部署](#验证部署)
- [运维命令](#运维命令)
- [故障排查](#故障排查)
- [安全建议](#安全建议)

---

## 系统要求

### 硬件要求

| 配置 | 最低要求 | 推荐配置 |
|------|---------|---------|
| CPU | 2 核 | 4 核 |
| 内存 | 2 GB | 4 GB |
| 磁盘 | 20 GB | 50 GB |
| 网络 | 1 Mbps | 10 Mbps |

### 软件要求

- **操作系统**: Ubuntu 20.04+ / Debian 11+ / CentOS 8+ / 其他主流 Linux 发行版
- **Docker**: 20.10+
- **Docker Compose**: V2 (2.0+)

---

## 快速部署

如果你已经熟悉 Docker，可以使用一键部署脚本：

```bash
# 1. 上传代码到服务器
scp -r . user@服务器IP:/opt/wheretogo

# 2. SSH 登录服务器
ssh user@服务器IP

# 3. 进入项目目录
cd /opt/wheretogo

# 4. 配置环境变量
cp .env.production .env
nano .env  # 填入 API Keys 和数据库密码

# 5. 一键部署
./deploy.sh

# 6. 访问应用
# 前端: http://服务器IP:3000
# BFF:  http://服务器IP:8000
```

---

## 详细步骤

### 1. 安装 Docker

#### Ubuntu / Debian

```bash
# 更新包索引
sudo apt-get update

# 安装依赖
sudo apt-get install -y ca-certificates curl gnupg

# 添加 Docker 官方 GPG 密钥
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 添加 Docker 仓库
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 安装 Docker
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 将当前用户添加到 docker 组（免 sudo）
sudo usermod -aG docker $USER

# 重新登录生效（或执行: newgrp docker）
exit
```

#### CentOS / RHEL

```bash
# 安装依赖
sudo yum install -y yum-utils

# 添加 Docker 仓库
sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo

# 安装 Docker
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 启动 Docker
sudo systemctl start docker
sudo systemctl enable docker

# 将当前用户添加到 docker 组
sudo usermod -aG docker $USER

# 重新登录生效
exit
```

#### 验证安装

```bash
docker --version
# 应输出: Docker version 24.x.x 或更高

docker compose version
# 应输出: Docker Compose version v2.x.x 或更高
```

---

### 2. 上传代码

#### 方式 1: 使用 Git（推荐）

```bash
# 在服务器上克隆代码
cd /opt
git clone <你的Git仓库地址> wheretogo
cd wheretogo
```

#### 方式 2: 使用 SCP 上传

**在本地执行**:

```bash
# 打包代码（排除不必要的文件）
tar -czf wheretogo.tar.gz \
  --exclude=node_modules \
  --exclude=.venv \
  --exclude=__pycache__ \
  --exclude=.next \
  --exclude=.git \
  --exclude=.pytest_cache \
  --exclude=.ruff_cache \
  .

# 上传到服务器
scp wheretogo.tar.gz user@服务器IP:/opt/
```

**在服务器执行**:

```bash
cd /opt
tar -xzf wheretogo.tar.gz
mv WhereToGo2 wheretogo  # 如果目录名不同
cd wheretogo
```

---

### 3. 配置环境变量

```bash
# 复制配置模板
cp .env.production .env

# 编辑配置文件
nano .env
```

#### 必填配置项

```bash
# 数据库密码（必填，请使用强密码）
WTG_DB_PASSWORD=YourStrongPassword123!

# LLM API Key（必填，否则功能受限）
WTG_LLM_API_KEY=sk-your-actual-llm-key

# Embedding API Key（必填）
WTG_EMBEDDING_API_KEY=sk-your-actual-embedding-key

# 搜索 API Key（必填，深度研究需要）
WTG_SEARCH_API_KEY=tvly-your-actual-search-key

# 前端访问 BFF 的地址（必填，修改为服务器实际 IP）
NEXT_PUBLIC_API_BASE=http://192.168.1.100:8000
```

#### 可选配置项

```bash
# 高德地图 API Key（可选）
WTG_AMAP_KEY=your-amap-key

# 和风天气 API Key（可选）
WTG_QWEATHER_KEY=your-qweather-key

# 日志级别（可选，默认 INFO）
WTG_LOG_LEVEL=INFO
```

保存并退出（nano: `Ctrl+O` → `Enter` → `Ctrl+X`）

---

### 4. 执行部署

#### 方式 1: 使用一键部署脚本（推荐）

```bash
./deploy.sh
```

脚本会自动执行以下操作：
1. 检查 Docker 和 Docker Compose
2. 检查 .env 配置
3. 构建所有 Docker 镜像
4. 启动 PostgreSQL 和 Redis
5. 运行数据库迁移
6. 加载种子数据
7. 启动所有服务
8. 验证部署

#### 方式 2: 手动执行

```bash
# 1. 构建镜像
docker compose -f docker-compose.prod.yml build

# 2. 启动数据库和 Redis
docker compose -f docker-compose.prod.yml up -d postgres redis

# 3. 等待数据库就绪（约 10 秒）
docker compose -f docker-compose.prod.yml ps

# 4. 运行数据库迁移
docker compose -f docker-compose.prod.yml run --rm bff uv run alembic upgrade head

# 5. 加载种子数据
docker compose -f docker-compose.prod.yml run --rm bff uv run python -m wheretogo.seeds.loader

# 6. 启动所有服务
docker compose -f docker-compose.prod.yml up -d
```

---

### 5. 配置防火墙

#### Ubuntu / Debian (ufw)

```bash
# 开放端口
sudo ufw allow 3000/tcp  # 前端
sudo ufw allow 8000/tcp  # BFF API

# 启用防火墙
sudo ufw enable

# 查看状态
sudo ufw status
```

#### CentOS / RHEL (firewalld)

```bash
# 开放端口
sudo firewall-cmd --permanent --add-port=3000/tcp
sudo firewall-cmd --permanent --add-port=8000/tcp

# 重新加载
sudo firewall-cmd --reload

# 查看状态
sudo firewall-cmd --list-all
```

---

## 配置说明

### 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Web 前端 | 3000 | Next.js 用户界面 |
| BFF API | 8000 | FastAPI 后端接口 |
| PostgreSQL | 5433 | 数据库（Docker 内部 5432） |
| Redis | 6380 | 缓存（Docker 内部 6379） |

### 数据持久化

数据存储在 Docker volumes 中：

```bash
# 查看数据卷
docker volume ls | grep wheretogo

# 输出:
# wheretogo_pgdata      # PostgreSQL 数据
# wheretogo_redisdata   # Redis 数据
```

**删除容器不会丢失数据**，只有执行 `docker compose down -v` 才会删除数据卷。

---

## 验证部署

### 1. 检查服务状态

```bash
docker compose -f docker-compose.prod.yml ps
```

应该看到 5 个服务都是 `Up` 状态：

```
NAME                  STATUS
wheretogo-postgres    Up (healthy)
wheretogo-redis       Up (healthy)
wheretogo-bff         Up
wheretogo-worker      Up
wheretogo-web         Up
```

### 2. 测试 BFF 健康检查

```bash
curl http://localhost:8000/health
```

应返回：

```json
{"ok": true}
```

### 3. 访问前端

在浏览器中访问：

```
http://服务器IP:3000
```

应该能看到「周末去哪儿」首页。

### 4. 查看日志

```bash
# 查看所有服务日志
docker compose -f docker-compose.prod.yml logs -f

# 查看特定服务日志
docker compose -f docker-compose.prod.yml logs -f bff
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f web
```

---

## 运维命令

### 启动 / 停止 / 重启

```bash
# 启动所有服务
docker compose -f docker-compose.prod.yml up -d

# 停止所有服务
docker compose -f docker-compose.prod.yml down

# 重启所有服务
docker compose -f docker-compose.prod.yml restart

# 重启特定服务
docker compose -f docker-compose.prod.yml restart bff
```

### 查看日志

```bash
# 实时日志（所有服务）
docker compose -f docker-compose.prod.yml logs -f

# 实时日志（特定服务）
docker compose -f docker-compose.prod.yml logs -f bff

# 最近 100 行日志
docker compose -f docker-compose.prod.yml logs --tail=100 bff
```

### 进入容器

```bash
# 进入 BFF 容器
docker exec -it wheretogo-bff bash

# 进入数据库容器
docker exec -it wheretogo-postgres psql -U wheretogo -d wheretogo

# 进入 Redis 容器
docker exec -it wheretogo-redis redis-cli
```

### 数据库操作

```bash
# 运行迁移
docker compose -f docker-compose.prod.yml run --rm bff uv run alembic upgrade head

# 回滚迁移
docker compose -f docker-compose.prod.yml run --rm bff uv run alembic downgrade -1

# 重新加载种子数据
docker compose -f docker-compose.prod.yml run --rm bff uv run python -m wheretogo.seeds.loader
```

### 清理

```bash
# 停止并删除容器（保留数据）
docker compose -f docker-compose.prod.yml down

# 停止并删除容器和数据（慎用！）
docker compose -f docker-compose.prod.yml down -v

# 清理未使用的镜像
docker system prune -a
```

### 资源监控

```bash
# 查看资源占用
docker stats

# 查看磁盘占用
docker system df
```

---

## 故障排查

### 问题 1: 容器启动失败

**症状**: `docker compose ps` 显示服务为 `Exit` 状态

**排查**:

```bash
# 查看容器日志
docker compose -f docker-compose.prod.yml logs bff

# 常见原因:
# 1. 环境变量配置错误 → 检查 .env 文件
# 2. 数据库连接失败 → 确保 postgres 服务健康
# 3. 端口被占用 → sudo lsof -i :8000
```

### 问题 2: 前端无法访问后端

**症状**: 前端页面显示"网络异常"或"无法连接到服务器"

**排查**:

```bash
# 1. 检查 NEXT_PUBLIC_API_BASE 配置
cat .env | grep NEXT_PUBLIC_API_BASE

# 应该是服务器实际 IP，例如:
# NEXT_PUBLIC_API_BASE=http://192.168.1.100:8000

# 2. 检查 BFF 是否正常运行
curl http://localhost:8000/health

# 3. 检查防火墙
sudo ufw status  # Ubuntu/Debian
sudo firewall-cmd --list-all  # CentOS/RHEL

# 4. 修改配置后重启前端
docker compose -f docker-compose.prod.yml restart web
```

### 问题 3: 数据库连接失败

**症状**: BFF 日志显示 `connection refused` 或 `could not connect to server`

**排查**:

```bash
# 1. 检查数据库是否健康
docker compose -f docker-compose.prod.yml ps postgres

# 2. 查看数据库日志
docker compose -f docker-compose.prod.yml logs postgres

# 3. 测试数据库连接
docker exec -it wheretogo-postgres psql -U wheretogo -d wheretogo -c "SELECT 1;"

# 4. 检查密码是否正确
cat .env | grep WTG_DB_PASSWORD
```

### 问题 4: 内存不足

**症状**: 容器频繁重启或 `docker stats` 显示内存使用率 100%

**解决**:

```bash
# 1. 增加服务器内存（推荐 4GB+）

# 2. 或者减少服务资源限制
# 编辑 docker-compose.prod.yml，调整 deploy.resources.limits.memory

# 3. 清理 Docker 缓存
docker system prune -a
```

### 问题 5: 镜像构建失败

**症状**: `docker compose build` 报错

**排查**:

```bash
# 1. 检查网络连接（需要下载依赖）
ping pypi.org
ping registry.npmjs.org

# 2. 清理构建缓存
docker compose -f docker-compose.prod.yml build --no-cache

# 3. 查看详细构建日志
docker compose -f docker-compose.prod.yml build --progress=plain
```

---

## 安全建议

### 1. 修改默认密码

```bash
# .env 文件
WTG_DB_PASSWORD=使用强密码生成器生成
WTG_ICS_TOKEN_SECRET=使用随机字符串
```

### 2. 限制端口访问

```bash
# 只允许特定 IP 访问（如果需要）
sudo ufw allow from 你的IP地址 to any port 3000
sudo ufw allow from 你的IP地址 to any port 8000
```

### 3. 使用 HTTPS（可选）

如果需要 HTTPS，建议使用 Nginx 反向代理 + Let's Encrypt 证书。

### 4. 定期备份

```bash
# 备份数据库
docker exec wheretogo-postgres pg_dump -U wheretogo wheretogo > backup_$(date +%Y%m%d).sql

# 备份数据卷
docker run --rm -v wheretogo_pgdata:/data -v $(pwd):/backup alpine tar czf /backup/pgdata_backup.tar.gz -C /data .
```

### 5. 定期更新

```bash
# 拉取最新代码
git pull

# 重新构建镜像
docker compose -f docker-compose.prod.yml build

# 重启服务
docker compose -f docker-compose.prod.yml up -d
```

---

## 后续优化

### 1. 配置 Nginx 反向代理（可选）

如果需要域名访问和 HTTPS，可以配置 Nginx：

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://localhost:3000;
    }

    location /api {
        proxy_pass http://localhost:8000;
    }
}
```

### 2. 配置 systemd 开机自启（可选）

创建 `/etc/systemd/system/wheretogo.service`:

```ini
[Unit]
Description=WhereToGo Docker Compose
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/wheretogo
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down

[Install]
WantedBy=multi-user.target
```

启用:

```bash
sudo systemctl enable wheretogo
sudo systemctl start wheretogo
```

---

## 获取帮助

如果遇到问题：

1. 查看日志: `docker compose -f docker-compose.prod.yml logs -f`
2. 检查配置: `cat .env`
3. 查看本文档的[故障排查](#故障排查)部分

---

## 附录

### API Key 获取地址

- **通义千问 (LLM)**: https://dashscope.console.aliyun.com/
- **SiliconFlow (Embedding)**: https://siliconflow.cn/
- **Tavily (搜索)**: https://tavily.com/
- **高德地图**: https://lbs.amap.com/
- **和风天气**: https://dev.qweather.com/

### 相关文档

- [项目 README](../README.md)
- [技术架构文档](../技术方案/)
- [API 接口文档](../技术方案/接口文档_BFF_API.md)
