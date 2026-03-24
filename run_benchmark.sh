#!/bin/bash

# OpenAI API 性能测试启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 打印信息
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查依赖
check_dependencies() {
    print_info "检查依赖..."

    if ! command -v python3 &> /dev/null; then
        print_error "Python3 未安装"
        exit 1
    fi

    if ! command -v pip3 &> /dev/null; then
        print_error "pip3 未安装"
        exit 1
    fi

    print_info "依赖检查通过"
}

# 安装依赖
install_deps() {
    print_info "安装依赖..."

    pip3 install -q requests locust 2>/dev/null || true

    print_info "依赖安装完成"
}

# 运行数据库索引优化
run_migrations() {
    print_info "运行数据库索引优化..."

    python3 migrations/add_openai_api_indexes.py

    print_info "数据库优化完成"
}

# 运行基准测试
run_benchmark() {
    local scenario=$1
    local host=$2
    local token=$3

    print_info "运行基准测试: $scenario"
    print_info "目标: $host"

    case $scenario in
        low)
            python3 benchmark/simple_benchmark.py \
                --host "$host" \
                --token "$token" \
                --scenario low
            ;;
        medium)
            python3 benchmark/simple_benchmark.py \
                --host "$host" \
                --token "$token" \
                --scenario medium
            ;;
        high)
            python3 benchmark/simple_benchmark.py \
                --host "$host" \
                --token "$token" \
                --scenario high
            ;;
        extreme)
            python3 benchmark/simple_benchmark.py \
                --host "$host" \
                --token "$token" \
                --scenario extreme
            ;;
        all)
            python3 benchmark/simple_benchmark.py \
                --host "$host" \
                --token "$token" \
                --scenario all
            ;;
        locust)
            print_info "启动 Locust Web 界面..."
            locust -f benchmark/locustfile.py --host="$host"
            ;;
        *)
            print_error "未知场景: $scenario"
            print_info "可用场景: low, medium, high, extreme, all, locust"
            exit 1
            ;;
    esac
}

# 打印帮助信息
print_help() {
    cat << EOF
OpenAI API 性能测试工具

用法:
    ./run_benchmark.sh [命令] [选项]

命令:
    migrate         运行数据库索引优化
    test            运行基准测试
    help            显示帮助

测试选项:
    --scenario      测试场景 (low|medium|high|extreme|all|locust)
    --host          API host (默认: http://localhost:50110)
    --token         API token

示例:
    # 运行数据库优化
    ./run_benchmark.sh migrate

    # 运行低并发测试
    ./run_benchmark.sh test --scenario low --token your-token

    # 运行所有测试
    ./run_benchmark.sh test --scenario all --token your-token

    # 启动Locust Web界面
    ./run_benchmark.sh test --scenario locust

测试场景说明:
    low         低并发 (100 QPS目标)
    medium      中并发 (500 QPS目标)
    high        高并发 (1000 QPS目标)
    extreme     极限 (3000 QPS目标)
    all         运行所有场景
    locust      Locust Web界面模式
EOF
}

# 主函数
main() {
    # 解析命令
    COMMAND=${1:-help}

    case $COMMAND in
        migrate)
            check_dependencies
            install_deps
            run_migrations
            ;;

        test)
            shift
            local scenario="all"
            local host="http://localhost:50110"
            local token=""

            while [[ $# -gt 0 ]]; do
                case $1 in
                    --scenario)
                        scenario="$2"
                        shift 2
                        ;;
                    --host)
                        host="$2"
                        shift 2
                        ;;
                    --token)
                        token="$2"
                        shift 2
                        ;;
                    *)
                        print_error "未知选项: $1"
                        exit 1
                        ;;
                esac
            done

            if [[ -z "$token" && "$scenario" != "locust" ]]; then
                print_warning "未提供 token，使用默认测试token"
                token="your-test-api-token-here"
            fi

            check_dependencies
            install_deps
            run_benchmark "$scenario" "$host" "$token"
            ;;

        help|--help|-h)
            print_help
            ;;

        *)
            print_error "未知命令: $COMMAND"
            print_help
            exit 1
            ;;
    esac
}

main "$@"
