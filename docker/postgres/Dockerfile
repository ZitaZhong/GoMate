# 「周末去哪儿」隔离数据库镜像
# 目标：PostgreSQL 16 + PostGIS 3.4 + pgvector 0.8.x，单镜像满足 DD-01 §2 扩展清单。
#
# 为什么用多阶段 COPY 而非 apt：
#   构建环境无法访问 deb.debian.org（apt 失败），但本地已缓存官方 pgvector 镜像；
#   直接从 pgvector 官方镜像拷贝已编译好的扩展文件到 PostGIS 镜像，零 apt、零额外拉取。
# 说明：本镜像仅用于本项目独立实例，绝不复用本地已有的 PostgreSQL 生产实例。

FROM pgvector/pgvector:pg16 AS pgv

FROM postgis/postgis:16-3.4

# 从 pgvector 官方镜像拷贝已编译扩展（vector.so + 扩展 SQL/control 文件）
COPY --from=pgv /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/
COPY --from=pgv /usr/share/postgresql/16/extension/vector.control /usr/share/postgresql/16/extension/
COPY --from=pgv /usr/share/postgresql/16/extension/vector--*.sql /usr/share/postgresql/16/extension/

# pgvectorscale 仅在活跃向量 > ~5000 万时才需要（DD-01 §10.3），v0.1 用 HNSW 即可。
# 迁移脚本对 vectorscale 扩展做“可选安装”，缺失不影响主流程。
