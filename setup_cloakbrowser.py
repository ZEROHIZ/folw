"""
核心职责：CloakBrowser 本地环境自动补丁与内核安装脚本。
主要功能：
1. 将 CloakBrowser 缓存目录固定在当前虚拟环境内（venv/browsers/cloakbrowser），避免跨盘/权限冲突；
2. 为 CloakBrowser 下载器注入分流策略：516MB 内核包强制直连高速下载（trust_env=False），SHA256SUMS 签名校验支持直连+代理双通道回退；
3. 自动写入 License Key 并执行 `python -m cloakbrowser install` 完成内核下载与校验。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def ensure_cloakbrowser_pip_package() -> None:
    """确保当前 Python 环境中已安装 cloakbrowser 依赖包。"""
    try:
        import cloakbrowser  # noqa: F401
        return
    except ImportError:
        pass

    print("[*] 未检测到 cloakbrowser Python 包，正在自动安装...")
    for mirror in (None, "https://pypi.tuna.tsinghua.edu.cn/simple"):
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "cloakbrowser"]
        if mirror:
            cmd.extend(["-i", mirror])
        res = subprocess.run(cmd)
        if res.returncode == 0:
            import importlib
            importlib.invalidate_caches()
            print("[✓] cloakbrowser Python 包安装成功")
            return
    raise RuntimeError("cloakbrowser Python 包自动安装失败，请检查网络连接。")


def patch_cloakbrowser_package() -> Path:
    """对当前 Python 环境中的 cloakbrowser 包应用本地缓存目录与直连下载分流补丁。"""
    ensure_cloakbrowser_pip_package()
    import cloakbrowser
    import cloakbrowser.config as cb_config
    import cloakbrowser.download as cb_download

    pkg_dir = Path(cloakbrowser.__file__).resolve().parent
    config_file = Path(cb_config.__file__).resolve()
    download_file = Path(cb_download.__file__).resolve()

    # 1. Patch config.py: store browsers inside <venv>/browsers/cloakbrowser
    config_text = config_file.read_text(encoding="utf-8")
    target_cache_fn = """def get_default_cache_dir() -> Path:
    env_dir = os.environ.get("CLOAKBROWSER_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".cloakbrowser"
"""
    replacement_cache_fn = """def get_default_cache_dir() -> Path:
    env_dir = os.environ.get("CLOAKBROWSER_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    return Path(sys.prefix) / "browsers" / "cloakbrowser"
"""
    if "import sys" not in config_text:
        config_text = "import sys\n" + config_text
    if target_cache_fn in config_text:
        config_text = config_text.replace(target_cache_fn, replacement_cache_fn)
        config_file.write_text(config_text, encoding="utf-8")
        print(f"[✓] 已更新缓存路径配置: {config_file}")
    elif 'Path(sys.prefix) / "browsers" / "cloakbrowser"' in config_text:
        print(f"[✓] 缓存路径配置已就绪: {config_file}")

    # 2. Patch download.py: _download_file uses trust_env=False (direct high-speed download)
    #    and _verify_pro_download tries direct first, then proxy fallback for SHA256SUMS.
    download_text = download_file.read_text(encoding="utf-8")
    old_verify = """    with httpx.Client(follow_redirects=True, timeout=30) as client:
        sums_resp = client.get(f"{base_url}/SHA256SUMS", headers=headers)
        sums_resp.raise_for_status()
        sig_resp = client.get(f"{base_url}/SHA256SUMS.sig", headers=headers)
        sig_resp.raise_for_status()"""

    new_verify = """    sums_resp = None
    sig_resp = None
    last_err = None
    for use_env_proxy in (False, True):
        try:
            with httpx.Client(follow_redirects=True, timeout=30, trust_env=use_env_proxy) as client:
                sums_resp = client.get(f"{base_url}/SHA256SUMS", headers=headers)
                sums_resp.raise_for_status()
                sig_resp = client.get(f"{base_url}/SHA256SUMS.sig", headers=headers)
                sig_resp.raise_for_status()
                break
        except Exception as exc:
            last_err = exc
    if sums_resp is None or sig_resp is None:
        raise last_err"""

    modified_download = False
    if "with httpx.stream(\"GET\", url, follow_redirects=True, timeout=120, headers=headers) as response:" in download_text:
        download_text = download_text.replace(
            'with httpx.stream("GET", url, follow_redirects=True, timeout=120, headers=headers) as response:',
            "with httpx.Client(follow_redirects=True, timeout=120, trust_env=False) as _dl_client, _dl_client.stream(\"GET\", url, headers=headers) as response:",
        )
        modified_download = True

    if old_verify in download_text:
        download_text = download_text.replace(old_verify, new_verify)
        modified_download = True

    if modified_download:
        download_file.write_text(download_text, encoding="utf-8")
        print(f"[✓] 已注入大文件直连加速与校验回退补丁: {download_file}")
    else:
        print(f"[✓] 下载器分流补丁已就绪: {download_file}")

    return pkg_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="安装并配置 CloakBrowser Pro 内核")
    parser.add_argument(
        "--key",
        default=os.environ.get("CLOAKBROWSER_LICENSE_KEY", ""),
        help="CloakBrowser License Key (默认使用环境变量 CLOAKBROWSER_LICENSE_KEY，可逗号/换行分隔多个)",
    )
    parser.add_argument(
        "--skip-browser-download",
        action="store_true",
        help="仅打补丁并保存 Key，跳过浏览器二进制下载",
    )
    args = parser.parse_args()

    patch_cloakbrowser_package()

    from cloakbrowser.license import save_license_key

    raw_keys = [
        part.strip()
        for part in str(args.key or "").replace(",", "\n").splitlines()
        if part.strip()
    ]
    if raw_keys:
        primary_key = raw_keys[0]
        key_path = save_license_key(primary_key)
        os.environ["CLOAKBROWSER_LICENSE_KEY"] = primary_key
        print(f"[✓] 已写入主 License Key ({primary_key[:7]}...{primary_key[-4:]}) -> {key_path}")

    if args.skip_browser_download:
        return 0

    print("[*] 正在检查并下载 CloakBrowser Pro 内核（直连高速通道）...")
    cmd = [sys.executable, "-m", "cloakbrowser", "install"]
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

