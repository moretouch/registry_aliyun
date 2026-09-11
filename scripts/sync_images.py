#!/usr/bin/env python3
"""
批量同步公开/私有镜像到阿里云 ACR，自动跳过未变化的镜像。

- 使用 crane (go-containerregistry) 做 registry-to-registry 拷贝
- 通过 --platform 过滤掉 attestation manifest（ACR 不支持 OCI empty manifest）
- 内置重试，容忍 ghcr.io 偶发的 PROTOCOL_ERROR
- 通过比较逐平台 manifest digest 判断是否变化（而非易变的 index digest）
"""
import sys
import os
import json
import time
import subprocess
import functools
import yaml

# 立即 flush，避免 GitHub Actions 里输出卡缓冲、看起来像"假死"
print = functools.partial(print, flush=True)

DEBUG = True


def dlog(*args, **kwargs):
    if DEBUG:
        print("\033[35m[DEBUG]\033[0m", *args, **kwargs)


def run(cmd: str, check: bool = False, debug: bool = True) -> subprocess.CompletedProcess:
    print(f"\033[36m[CMD]\033[0m {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if DEBUG and debug:
        if result.stdout:
            print(result.stdout, end='' if result.stdout.endswith('\n') else '\n')
        if result.stderr:
            print(result.stderr, end='' if result.stderr.endswith('\n') else '\n')
        print(f"\033[35m[EXIT]\033[0m {result.returncode}")
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd}\n{result.stderr}")
    return result


# ---------------------------------------------------------------------------
# crane 封装
# ---------------------------------------------------------------------------

def check_crane():
    r = subprocess.run("crane version", shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print("\033[31m错误: 未找到 crane 命令。\033[0m")
        print("安装: https://github.com/google/go-containerregistry/releases")
        sys.exit(1)
    print(f"\033[32mcrane version: {r.stdout.strip()}\033[0m")


def crane_manifest(image: str):
    """返回 manifest JSON dict，失败返回 None。"""
    r = run(f"crane manifest {image}", debug=False)
    if r.returncode != 0:
        dlog(f"crane manifest {image} 失败: rc={r.returncode} stderr={r.stderr.strip()!r}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        dlog(f"crane manifest 输出无法解析为 JSON: {r.stdout[:200]!r}")
        return None


def crane_digest(image: str):
    """返回镜像的 digest（sha256:...），失败返回 None。"""
    r = run(f"crane digest {image}", debug=False)
    if r.returncode != 0:
        return None
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    return lines[-1] if lines else None


def get_platform_digests(image: str):
    """
    解析镜像 index 得到 {platform: digest} 字典，跳过 attestation manifest
    （platform=unknown/unknown）。如果 image 不是 index（单 manifest），返回 None。
    """
    m = crane_manifest(image)
    if not m:
        return None
    manifests = m.get('manifests')
    if not manifests:
        return None
    result = {}
    for entry in manifests:
        plat = entry.get('platform', {}) or {}
        os_name = plat.get('os', '')
        arch = plat.get('architecture', '')
        if os_name == 'unknown' or arch == 'unknown':
            # attestation manifest，ACR 不接受，跳过
            continue
        key = f"{os_name}/{arch}"
        variant = plat.get('variant')
        if variant:
            key += f"/{variant}"
        result[key] = entry['digest']
    return result


def crane_copy(source: str, target: str, platforms: str, max_retries: int = 3) -> bool:
    """
    用 crane copy 做 registry-to-registry 拷贝，带重试。
    platforms 形如 'linux/amd64,linux/arm64'，会拆成多个 --platform 参数。
    """
    plat_parts = [p.strip() for p in platforms.split(',') if p.strip()]
    plat_flags = ' '.join(f'--platform {p}' for p in plat_parts)
    cmd = f"crane copy {plat_flags} {source} {target}".strip()

    for attempt in range(1, max_retries + 1):
        r = run(cmd)
        if r.returncode == 0:
            return True
        dlog(f"crane copy 第 {attempt} 次失败，rc={r.returncode}")
        if attempt < max_retries:
            wait = 5 * attempt
            print(f"\033[33m[RETRY] {wait}s 后重试 ({attempt}/{max_retries})...\033[0m")
            time.sleep(wait)
    return False


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def main(config_file: str):
    check_crane()

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

    # 先登录默认 ACR
    login_acr(default_region)

    # 源仓库凭据（如果需要拉私有源镜像）
    if source_user and source_pass:
        src_reg = os.environ.get('SOURCE_REGISTRY', '')
        if src_reg.startswith('ghcr.io'):
            run(f"echo {source_pass} | docker login ghcr.io --username {source_user} --password-stdin")
        elif src_reg:
            # 明确指定了 registry 域名
            run(f"echo {source_pass} | docker login {src_reg} --username {source_user} --password-stdin")
        else:
            # Docker Hub
            run(f"echo {source_pass} | docker login --username {source_user} --password-stdin")

    success, skipped = 0, 0
    fail_list = []

    for item in images:
        source = item.get('source', '').strip()
        target = item.get('target', '').strip()
        region = item.get('region', default_region)
        platforms = item.get('platforms', 'linux/amd64,linux/arm64')

        if not source or not target:
            print(f"跳过无效条目: {item}")
            continue

        print(f"\n{'='*72}")
        print(f"--- {source}  ->  {target}  (region={region}, platforms={platforms}) ---")
        print(f"{'='*72}")

        login_acr(region)
        target_full = f"registry.{region}.aliyuncs.com/{namespace}/{target}"

        wanted = set(p.strip() for p in platforms.split(',') if p.strip())

        # ---- 1) 获取源/目标各平台的 manifest digest ----
        src_plats = get_platform_digests(source)
        dst_plats = get_platform_digests(target_full)

        if src_plats is None:
            # 源不是 index（单 manifest），退化为整体 digest 比较
            print(f"\033[33m[NOTE] 源镜像不是 index（单 manifest），使用整体 digest 比较\033[0m")
            src_d = crane_digest(source)
            dst_d = crane_digest(target_full)
            print(f"    source digest: {src_d}")
            print(f"    target digest: {dst_d}")
            if src_d and src_d == dst_d:
                print(f"\033[32m镜像未变化，跳过同步\033[0m")
                skipped += 1
                continue
            if crane_copy(source, target_full, platforms):
                print(f"\033[32m[OK] 同步成功: {source} -> {target_full}\033[0m")
                success += 1
            else:
                print(f"\033[31m同步失败: {source} -> {target_full}\033[0m")
                fail_list.append(source)
            continue

        def filter_wanted(d):
            if not d:
                return {}
            return {k: v for k, v in d.items() if k in wanted}

        src_f = filter_wanted(src_plats)
        dst_f = filter_wanted(dst_plats)

        print(f"  源所有平台: {src_plats}")
        print(f"  目标所有平台: {dst_plats}")
        print(f"  比较（仅关注 {sorted(wanted)}）:")
        print(f"    source: {src_f}")
        print(f"    target: {dst_f}")

        if src_f and dst_f and src_f == dst_f:
            print(f"\033[32m镜像未变化，跳过同步: {source}\033[0m")
            skipped += 1
            continue

        # ---- 2) 执行 crane copy ----
        print(f"\033[36m>>> 开始同步（crane copy）\033[0m")
        if not crane_copy(source, target_full, platforms):
            print(f"\033[31m同步失败: {source} -> {target_full}\033[0m")
            fail_list.append(source)
            continue

        # ---- 3) 验证 ----
        new_dst = filter_wanted(get_platform_digests(target_full))
        if new_dst == src_f:
            print(f"\033[32m[OK] 同步成功且校验通过: {source} -> {target_full}\033[0m")
        else:
            print(f"\033[33m[WARN] 同步完成但校验不一致\033[0m")
            print(f"    expected: {src_f}")
            print(f"    got:      {new_dst}")
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
