#!/usr/bin/env python3
"""批量同步公开/私有镜像到阿里云 ACR，自动跳过未变化的镜像。"""
import sys
import os
import re
import subprocess
import json
import yaml

def run(cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    """执行 shell 命令，可选择是否在失败时抛出异常。"""
    print(f"\033[36m[CMD]\033[0m {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd}\n{result.stderr}")
    return result

def get_digest(image: str, registry: str = "", user: str = "", password: str = "") -> str:
    """
    获取远程镜像的 manifest digest（sha256:...）。
    如果获取失败返回 None。
    """
    # 如果提供了 registry 登录信息，先临时登录（可能影响全局状态，调用方需注意）
    # 实际上为了不影响后续，我们可以在这里临时登录再 logout，但会增加复杂度。
    # 这里改为由调用方确保已登录对应 registry。
    cmd = f"docker buildx imagetools inspect {image}"
    res = run(cmd)
    if res.returncode != 0:
        # 尝试捕获常见错误，如未登录或不存在
        print(res.stderr, file=sys.stderr)
        return None
    _backup = None
    for line in res.stdout.splitlines():
        if line.startswith('Digest:'):
            return line.split('Digest:')[1].strip()
        # 匹配如果有一个64位的16进制字符
        if _backup is None:
            _match = re.search('[0-9a-f]{64}', line)
            if _match:
                _backup = _match.group()
                print(f"匹配到: {_backup}")
    return _backup


def main(config_file: str):
    # 读取 YAML 配置
    with open(config_file, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    images = config.get('images', [])
    if not images:
        print("未找到任何镜像配置，退出。")
        return

    namespace = os.environ['REGISTRY_NAMESPACE']
    user = os.environ['REGISTRY_USER']
    password = os.environ['REGISTRY_PASS']
    default_region = os.environ.get('ACR_REGION_DEFAULT', 'cn-hangzhou')

    # 可选源仓库登录信息（仅当镜像源为私有仓库时配置）
    source_user = os.environ.get('SOURCE_REGISTRY_USER', '')
    source_pass = os.environ.get('SOURCE_REGISTRY_PASS', '')

    # 当前已登录的 ACR region 记录
    logged_acr = ""

    def login_acr(region: str):
        """登录到指定 region 的 ACR，避免重复登录。"""
        nonlocal logged_acr
        if logged_acr == region:
            return
        registry = f"registry.{region}.aliyuncs.com"
        cmd = f"echo {password} | docker login --username {user} --password-stdin {registry}"
        run(cmd, check=True)
        logged_acr = region

    # 先登录默认 ACR（方便后续 inspect 目标镜像）
    login_acr(default_region)

    # 如果配置了源仓库凭据，尝试登录 Docker Hub 或 ghcr.io（仅一次）
    if source_user and source_pass:
        # 简单起见，尝试同时登录 Docker Hub 和 GitHub Container Registry
        # 实际可根据 source 的域名动态判断，这里做通用处理
        if "ghcr.io" in source_user or os.environ.get('SOURCE_REGISTRY','').startswith('ghcr.io'):
            run(f"echo {source_pass} | docker login ghcr.io --username {source_user} --password-stdin")
        else:
            # Docker Hub
            run(f"echo {source_pass} | docker login --username {source_user} --password-stdin")

    success = 0
    skipped = 0
    fail_list = []

    for item in images:
        source = item['source']
        target = item['target']          # 格式: 仓库名:标签，如 docker_hub:llama.cpp_server-cuda
        region = item.get('region', default_region)

        if not source or not target:
            print(f"跳过无效条目: {item}")
            continue

        print(f"\n--- 处理 {source} -> {target} (区域: {region}) ---")

        # 1) 获取源镜像 digest（需要该 registry 可访问）
        src_digest = get_digest(source)
        if not src_digest:
            # 可能是私有镜像或获取失败，尝试登录源仓库后再试一次（如果已配置）
            if source_user and source_pass:
                # 简单重试，登录已在前面完成
                src_digest = get_digest(source)
            if not src_digest:
                print(f"\033[31m无法获取源镜像 digest: {source}\033[0m")
                fail_list.append(source)
                continue

        # 2) 获取目标镜像 digest（需登录对应 ACR region）
        login_acr(region)  # 确保登录状态
        target_full = f"registry.{region}.aliyuncs.com/{namespace}/{target}"
        dst_digest = get_digest(target_full)
        # 注意：目标镜像可能不存在，此时 dst_digest 为 None

        if dst_digest and dst_digest == src_digest:
            print(f"\033[32m镜像未变化，跳过同步: {source}\033[0m")
            skipped += 1
            continue

        # 3) 确认需要同步：拉取源镜像
        if not run(f"docker pull {source}").returncode == 0:
            fail_list.append(source)
            continue

        # 4) 打标签并推送
        if not run(f"docker tag {source} {target_full}").returncode == 0:
            fail_list.append(source)
            continue
        if not run(f"docker push {target_full}").returncode == 0:
            fail_list.append(source)
            continue

        print(f"\033[32m[OK] 同步成功: {source} -> {target_full}\033[0m")
        success += 1

    # 清理登录状态
    run("docker logout", check=False)

    print(f"\n===== 同步报告 =====")
    print(f"成功: {success}")
    print(f"跳过(未变化): {skipped}")
    print(f"失败: {len(fail_list)}")
    if fail_list:
        print("失败项:")
        for img in fail_list:
            print(f"  - {img}")
        sys.exit(1)

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python scripts/sync_images.py <config.yaml>")
        sys.exit(1)
    main(sys.argv[1])
