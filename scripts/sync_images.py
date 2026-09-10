#!/usr/bin/env python3
"""批量同步公开/私有镜像到阿里云 ACR，自动跳过未变化的镜像。"""
import sys
import os
import subprocess
import re
import yaml

DEBUG = true

def dlog(*args, **kwargs):
    if DEBUG:
        print("\033[35m[DEBUG]\033[0m", *args, **kwargs)

def run(cmd: str, check: bool = False, debug: bool = True) -> subprocess.CompletedProcess:
    print(f"\033[36m[CMD]\033[0m {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if DEBUG and debug:
        if result.stdout:
            print("\033[35m[STDOUT]\033[0m")
            print(result.stdout)
        if result.stderr:
            print("\033[35m[STDERR]\033[0m")
            print(result.stderr)
        print(f"\033[35m[EXIT]\033[0m {result.returncode}")
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd}\n{result.stderr}")
    return result

def get_digest(image: str) -> str:
    """
    用 docker buildx imagetools inspect 拿 digest。
    失败返回 None。
    """
    cmd = f"docker buildx imagetools inspect {image}"
    res = run(cmd)
    if res.returncode != 0:
        # 打印真实失败原因，区分“不存在”和“未授权/网络问题”
        print(f"\033[33m[WARN]\033[0m imagetools inspect 失败: {image}")
        print(f"        returncode={res.returncode}")
        print(f"        stderr={res.stderr.strip()!r}")
        return None

    dlog(f"inspect 输出 ({image}):")
    for line in res.stdout.splitlines():
        dlog(f"   | {line}")

    _backup = None
    for line in res.stdout.splitlines():
        if line.startswith('Digest:'):
            d = line.split('Digest:', 1)[1].strip()
            dlog(f"解析到 'Digest:' 行 -> {d}")
            return d
        if _backup is None and line:
            m = re.search(r'\b[0-9a-f]{64}\b', line)
            if m:
                _backup = m.group()
                dlog(f"备用匹配 -> {_backup}")
    dlog(f"最终返回(digest={_backup})")
    return _backup

def main(config_file: str):
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

    source_user = os.environ.get('SOURCE_REGISTRY_USER', '')
    source_pass = os.environ.get('SOURCE_REGISTRY_PASS', '')

    logged_acr = ""

    def login_acr(region: str):
        nonlocal logged_acr
        if logged_acr == region:
            return
        registry = f"registry.{region}.aliyuncs.com"
        run(f"echo {password} | docker login --username {user} --password-stdin {registry}",
            check=True)
        logged_acr = region

    login_acr(default_region)

    if source_user and source_pass:
        if "ghcr.io" in source_user or os.environ.get('SOURCE_REGISTRY', '').startswith('ghcr.io'):
            run(f"echo {source_pass} | docker login ghcr.io --username {source_user} --password-stdin")
        else:
            run(f"echo {source_pass} | docker login --username {source_user} --password-stdin")

    success = 0
    skipped = 0
    fail_list = []

    for item in images:
        source = item['source']
        target = item['target']
        region = item.get('region', default_region)

        if not source or not target:
            print(f"跳过无效条目: {item}")
            continue

        print(f"\n--- 处理 {source} -> {target} (区域: {region}) ---")

        # 1) 源 digest
        src_digest = get_digest(source)
        if not src_digest and source_user and source_pass:
            dlog("源 digest 为空，凭据已登录，重试一次")
            src_digest = get_digest(source)
        if not src_digest:
            print(f"\033[31m无法获取源镜像 digest: {source}\033[0m")
            fail_list.append(source)
            continue

        # 2) 目标 digest
        login_acr(region)
        target_full = f"registry.{region}.aliyuncs.com/{namespace}/{target}"
        dst_digest = get_digest(target_full)

        # ==== 关键 debug：把两端 digest 明明白白打出来 ====
        print(f"\033[35m[COMPARE]\033[0m")
        print(f"    source       : {source}")
        print(f"    source digest: {src_digest}")
        print(f"    target       : {target_full}")
        print(f"    target digest: {dst_digest}")
        print(f"    equal        : {src_digest == dst_digest}")
        # ================================================

        if dst_digest and dst_digest == src_digest:
            print(f"\033[32m镜像未变化，跳过同步: {source}\033[0m")
            skipped += 1
            continue

        # 3) 拉取源镜像
        r = run(f"docker pull {source}")
        if r.returncode != 0:
            print(f"\033[31m拉取失败: {source}\033[0m\n{r.stderr}")
            fail_list.append(source)
            continue

        # 4) 打标签并推送
        r = run(f"docker tag {source} {target_full}")
        if r.returncode != 0:
            print(f"\033[31m打标签失败: {source}\033[0m\n{r.stderr}")
            fail_list.append(source)
            continue

        r = run(f"docker push {target_full}")
        if r.returncode != 0:
            print(f"\033[31m推送失败: {target_full}\033[0m\n{r.stderr}")
            fail_list.append(source)
            continue

        # ==== 推送后再 inspect 一次目标，验证 digest 是否与源一致 ====
        if DEBUG:
            pushed_digest = get_digest(target_full)
            print(f"\033[35m[POST-PUSH]\033[0m target digest after push: {pushed_digest}")
            if pushed_digest and pushed_digest != src_digest:
                print(f"\033[33m[NOTE]\033[0m 推送后 digest 与源不一致 "
                      f"(src={src_digest}, dst={pushed_digest})，"
                      f"下次运行仍会重复同步！建议改用 "
                      f"`docker buildx imagetools create --tag {target_full} {source}`")
        # ============================================================

        print(f"\033[32m[OK] 同步成功: {source} -> {target_full}\033[0m")
        success += 1

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
