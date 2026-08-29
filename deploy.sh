#!/bin/bash
# 「周末去哪儿」一键部署脚本
# 
# 使用方法：
#   1. 确保已安装 Docker 和 Docker Compose
#   2. 复制 .env.production 为 .env 并填入实际配置
#   3. 执行: ./deploy.sh

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的消息
info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查命令是否存在
check_command() {
    if ! command -v $1 &> /dev/null; then
        error "$1 未安装，请先安装 $1"
        exit 1
    fi
}

# 打印标题
print_header() {
    echo ""
    echo "=========================================="
    echo "  $1"
    echo "=========================================="
    echo ""
}

# ==================== 前置检查 ====================
print_header "前置检查"

info "检查 Docker..."
check_command docker

info "检查 Docker Compose..."
if ! docker compose version &> /dev/null; then
    error "Docker Compose 未安装或版本过低，请安装 Docker Compose V2"
    exit 1
fi

success "Docker 和 Docker Compose 已安装"

# 检查 .env 文件
if [ ! -f .env ]; then
    warning ".env 文件不存在"
    if [ -f .env.production ]; then
        info "从 .env.production 复制..."
        cp .env.production .env
        warning "请编辑 .env 文件，填入实际的 API Keys 和配置"
        warning "然后重新运行本脚本"
        exit 1
    else
        error ".env.production 文件不存在，请先创建环境配置文件"
        exit 1
    fi
fi

success ".env 文件已存在"

# 检查必填配置
info "检查必填配置..."
source .env

if [ -z "$WTG_DB_PASSWORD" ] || [ "$WTG_DB_PASSWORD" = "YourStrongPassword123!" ]; then
    error "请在 .env 中设置 WTG_DB_PASSWORD（数据库密码）"
    exit 1
fi

if [ -z "$WTG_LLM_API_KEY" ] || [ "$WTG_LLM_API_KEY" = "sk-your-llm-api-key-here" ]; then
    warning "WTG_LLM_API_KEY 未配置，LLM 功能将使用离线兜底（效果较差）"
fi

if [ -z "$WTG_EMBEDDING_API_KEY" ] || [ "$WTG_EMBEDDING_API_KEY" = "sk-your-embedding-api-key-here" ]; then
    warning "WTG_EMBEDDING_API_KEY 未配置，Embedding 功能将使用离线兜底（效果较差）"
fi

if [ -z "$WTG_SEARCH_API_KEY" ] || [ "$WTG_SEARCH_API_KEY" = "tvly-your-search-api-key-here" ]; then
    warning "WTG_SEARCH_API_KEY 未配置，搜索功能将使用离线兜底（效果较差）"
fi

if [ -z "$NEXT_PUBLIC_API_BASE" ] || [ "$NEXT_PUBLIC_API_BASE" = "http://localhost:8000" ]; then
    warning "NEXT_PUBLIC_API_BASE 未配置，前端可能无法访问后端"
    warning "请在 .env 中设置为服务器实际 IP，例如: http://192.168.1.100:8000"
fi

success "配置检查完成"

# ==================== 构建镜像 ====================
print_header "构建 Docker 镜像"

info "构建 PostgreSQL 镜像..."
docker compose -f docker-compose.prod.yml build postgres

info "构建后端镜像（BFF + Worker）..."
docker compose -f docker-compose.prod.yml build bff worker

info "构建前端镜像..."
docker compose -f docker-compose.prod.yml build web

success "所有镜像构建完成"

# ==================== 启动基础设施 ====================
print_header "启动基础设施"

info "启动 PostgreSQL 和 Redis..."
docker compose -f docker-compose.prod.yml up -d postgres redis

info "等待数据库就绪..."
sleep 5

# 等待健康检查
for i in {1..30}; do
    if docker compose -f docker-compose.prod.yml ps | grep -c "healthy" | grep -q "2"; then
        success "PostgreSQL 和 Redis 已就绪"
        break
    fi
    if [ $i -eq 30 ]; then
        error "数据库启动超时，请检查日志: docker compose -f docker-compose.prod.yml logs postgres"
        exit 1
    fi
    echo -n "."
    sleep 2
done

# ==================== 数据库迁移 ====================
print_header "数据库迁移"

info "运行 Alembic 迁移..."
docker compose -f docker-compose.prod.yml run --rm bff uv run alembic upgrade head

success "数据库迁移完成"

# ==================== 加载种子数据 ====================
print_header "加载种子数据"

info "加载城市档案和来源注册表..."
docker compose -f docker-compose.prod.yml run --rm bff uv run python -m wheretogo.seeds.loader

success "种子数据加载完成"

# ==================== 启动所有服务 ====================
print_header "启动所有服务"

info "启动 BFF、Worker 和前端..."
docker compose -f docker-compose.prod.yml up -d

success "所有服务已启动"

# ==================== 验证部署 ====================
print_header "验证部署"

info "等待服务就绪..."
sleep 10

info "检查服务状态..."
docker compose -f docker-compose.prod.yml ps

info "测试 BFF 健康检查..."
if curl -f http://localhost:8000/health &> /dev/null; then
    success "BFF 健康检查通过"
else
    warning "BFF 健康检查失败，请查看日志: docker compose -f docker-compose.prod.yml logs bff"
fi

# ==================== 完成 ====================
print_header "部署完成"

echo ""
success "部署成功！"
echo ""
info "访问地址："
echo "  前端: http://localhost:3000"
echo "  BFF:  http://localhost:8000"
echo ""
info "如果在远程服务器上部署，请访问："
echo "  前端: http://服务器IP:3000"
echo "  BFF:  http://服务器IP:8000"
echo ""
info "常用命令："
echo "  查看日志: docker compose -f docker-compose.prod.yml logs -f"
echo "  查看状态: docker compose -f docker-compose.prod.yml ps"
echo "  重启服务: docker compose -f docker-compose.prod.yml restart"
echo "  停止服务: docker compose -f docker-compose.prod.yml down"
echo ""
info "更多信息请查看 DEPLOY.md"
echo ""
